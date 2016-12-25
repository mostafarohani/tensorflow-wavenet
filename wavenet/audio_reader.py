import fnmatch
import os
import random
import re
import threading
import spacy
import librosa
import numpy as np
import tensorflow as tf


nlp = spacy.load('en', vectors='en_glove_cc_300_1m_vectors')


def get_category_cardinality(files):
    id_reg_expression = re.compile(r'p([0-9]+)_([0-9]+)\.wav')
    min_id = None
    max_id = None
    for filename in files:
        matches = id_reg_expression.findall(filename)[0]
        id, recording_id = [int(id_) for id_ in matches]
        if id < min_id or min_id is None:
            min_id = id
        elif id > max_id or max_id is None:
            max_id = id

    return min_id, max_id


def randomize_files(files):
    for file in files:
        file_index = random.randint(0, (len(files) - 1))
        yield files[file_index]


def find_files(directory, pattern='*.wav'):
    '''Recursively finds all files matching the pattern.'''
    files = []
    for root, dirnames, filenames in os.walk(directory):
        for filename in fnmatch.filter(filenames, pattern):
            files.append(os.path.join(root, filename))
    return files


def label_text(texts, txt_reg_exp):
    labeled_texts = {}
    glove_dict = {}
    for text in texts:
        label = txt_reg_exp.findall(text)
        if label is not None and len(label) != 0:
            labeled_texts[label[0][1]] = text
            if label[0][1] not in glove_dict.keys():
                glove_dict[text] = nlp(u'%s' % text).vector.reshape((1, -1))

    return labeled_texts, glove_dict


def load_generic_audio(directory, sample_rate):
    '''Generator that yields audio waveforms from the directory.'''
    files = find_files(directory)
    texts = find_files(directory, pattern="*.txt")
    txt_reg_exp = re.compile(r'p([0-9]+)_([0-9]+)\.txt')
    get_text, glove_dict = label_text(texts, txt_reg_exp)
    id_reg_exp = re.compile(r'p([0-9]+)_([0-9]+)\.wav')
    print("files length: {}".format(len(files)))
    zero_vec = np.zeros((1, 300))
    randomized_files = randomize_files(files)
    for filename in randomized_files:
        ids = id_reg_exp.findall(filename)
        if ids is None or len(ids) == 0:
            continue
        ids = ids[0]
#        if ids[1] in get_text.keys():
#            text = get_text[ids[1]]
#            word_vec = glove_dict[text]
#        else:
        word_vec = zero_vec

        if ids is None:
            # The file name does not match the pattern containing ids, so
            # there is no id.
            category_id = None
        else:
            # The file name matches the pattern for containing ids.
            category_id = int(ids[0])
        audio, _ = librosa.load(filename, sr=sample_rate, mono=True)
        audio = audio.reshape(-1, 1)
        yield audio, filename, category_id, word_vec


def trim_silence(audio, threshold):
    '''Removes silence at the beginning and end of a sample.'''
    energy = librosa.feature.rmse(audio)
    frames = np.nonzero(energy > threshold)
    indices = librosa.core.frames_to_samples(frames)[1]

    # Note: indices can be an empty array, if the whole audio was silence.
    return audio[indices[0]:indices[-1]] if indices.size else audio[0:0]


def not_all_have_id(files):
    ''' Return true iff any of the filenames does not conform to the pattern
        we require for determining the category id.'''
    id_reg_exp = re.compile(r'p([0-9]+)_([0-9]+)\.wav')
    for file in files:
        ids = id_reg_exp.findall(file)
        if ids is None:
            return True
    return False


class AudioReader(object):

    '''Generic background audio reader that preprocesses audio files
    and enqueues them into a TensorFlow queue.'''

    def __init__(self,
                 audio_dir,
                 coord,
                 sample_rate,
                 gc_enabled,
                 sample_size=None,
                 silence_threshold=None,
                 queue_size=32):
        self.audio_dir = audio_dir
        self.sample_rate = sample_rate
        self.coord = coord
        self.sample_size = sample_size
        self.silence_threshold = silence_threshold
        self.gc_enabled = gc_enabled
        self.threads = []
        self.sample_placeholder = tf.placeholder(
            dtype=tf.float32, shape=(16000, 1))
        self.queue = tf.PaddingFIFOQueue(queue_size,
                                         ['float32'],
                                         shapes=[(16000, 1)])
        self.enqueue = self.queue.enqueue([self.sample_placeholder])

        if self.gc_enabled:
            self.id_placeholder = tf.placeholder(dtype=tf.int32, shape=())
            self.text_placeholder = tf.placeholder(
                dtype=tf.float32, shape=(1, 300))
            self.gc_queue = tf.PaddingFIFOQueue(queue_size, ['int32'],
                                                shapes=[()])
#            self.txt_queue = tf.PaddingFIFOQueue(queue_size, ['float32'],
#                                                 shapes=[(1, 300)])
            self.gc_enqueue = self.gc_queue.enqueue([self.id_placeholder])
#            self.txt_enqueue = self.txt_queue.enqueue([self.text_placeholder])

        # TODO Find a better way to check this.
        # Checking inside the AudioReader's thread makes it hard to terminate
        # the execution of the script, so we do it in the constructor for now.
        files = find_files(audio_dir)
        if not files:
            raise ValueError("No audio files found in '{}'.".format(audio_dir))
        if self.gc_enabled and not_all_have_id(files):
            raise ValueError("Global conditioning is enabled, but file names "
                             "do not conform to pattern having id.")
        # Determine the number of mutually-exclusive categories we will
        # accomodate in our embedding table.
        if self.gc_enabled:
            _, self.gc_category_cardinality = get_category_cardinality(files)
            # Add one to the largest index to get the number of categories,
            # since tf.nn.embedding_lookup expects zero-indexing. This
            # means one or more at the bottom correspond to unused entries
            # in the embedding lookup table. But that's a small waste of memory
            # to keep the code simpler, and preserves correspondance between
            # the id one specifies when generating, and the ids in the
            # file names.
            self.gc_category_cardinality += 1
            print("Detected --gc_cardinality={}".format(
                  self.gc_category_cardinality))
        else:
            self.gc_category_cardinality = None

    def dequeue(self, num_elements):
        output = self.queue.dequeue_many(num_elements)
        return output

    def dequeue_gc(self, num_elements):
        return self.gc_queue.dequeue_many(num_elements)

    def dequeue_txt(self, num_elements):
        return self.txt_queue.dequeue_many(num_elements)

    def thread_main(self, sess):
        buffer_ = np.array([])
        stop = False
        # Go through the dataset multiple times
        while not stop:
            iterator = load_generic_audio(self.audio_dir, self.sample_rate)
            for audio, filename, category_id, word_vec in iterator:
                if self.coord.should_stop():
                    stop = True
                    break
                if self.silence_threshold is not None:
                    # Remove silence
                    audio = trim_silence(audio[:, 0], self.silence_threshold)
                    audio = audio.reshape(-1, 1)[:16000]
                    if audio.size == 0:
                        print("Warning: {} was ignored as it contains only "
                              "silence. Consider decreasing trim_silence "
                              "threshold, or adjust volume of the audio."
                              .format(filename))
                if (len(audio[:, 0]) < 16000):
                    continue

                if self.sample_size:
                    # Cut samples into fixed size pieces
                    buffer_ = np.append(buffer_, audio)
                    while len(buffer_) > 0:
                        piece = np.reshape(buffer_[:self.sample_size], [-1, 1])
                        sess.run(self.enqueue,
                                 feed_dict={self.sample_placeholder: piece})
                        buffer_ = buffer_[self.sample_size:]
                        if self.gc_enabled:
                            sess.run(self.gc_enqueue,
                                     feed_dict={self.id_placeholder:
                                                category_id})
 #                           sess.run(self.txt_enqueue,
 #                                    feed_dict={self.text_placeholder:
 #                                               word_vec})
                else:
                    sess.run(self.enqueue,
                             feed_dict={self.sample_placeholder: audio})
                    if self.gc_enabled:
                        sess.run(self.gc_enqueue,
                                 feed_dict={self.id_placeholder:
                                            categeory_id})
#                        sess.run(self.txt_enqueue,
#                                 feed_dict={self.text_placeholder:
#                                            word_vec})

    def start_threads(self, sess, n_threads=1):
        for _ in range(n_threads):
            thread = threading.Thread(target=self.thread_main, args=(sess,))
            thread.daemon = True  # Thread will close when parent quits.
            thread.start()
            self.threads.append(thread)
        return self.threads
