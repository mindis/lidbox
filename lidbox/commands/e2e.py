import collections
import datetime
import hashlib
import importlib
import itertools
import json
import os
import random
import sys
import time

import kaldiio
import numpy as np
import tensorflow as tf

from lidbox import yaml_pprint
from lidbox.commands.base import BaseCommand, Command, ExpandAbspath
# from lidbox.metrics import AverageDetectionCost, AverageEqualErrorRate, AveragePrecision, AverageRecall
import lidbox.models as models
import lidbox.tf_data as tf_data
import lidbox.system as system


class E2E(BaseCommand):
    """TensorFlow pipeline for wavfile preprocessing, feature extraction and sound classification model training"""
    pass


def parse_space_separated(path):
    with open(path) as f:
        for l in f:
            l = l.strip()
            if l:
                yield l.split(' ')

def make_label2onehot(labels):
    labels_enum = tf.range(len(labels))
    # Label to int or one past last one if not found
    # TODO slice index out of bounds is probably not a very informative error message
    label2int = tf.lookup.StaticHashTable(
        tf.lookup.KeyValueTensorInitializer(
            tf.constant(labels),
            tf.constant(labels_enum)
        ),
        tf.constant(len(labels), dtype=tf.int32)
    )
    OH = tf.one_hot(labels_enum, len(labels))
    return label2int, OH

def config_checksum(config, datagroup_key):
    md5input = {k: config[k] for k in ("features", "datasets")}
    print("computing md5sum from")
    yaml_pprint(md5input)
    json_str = json.dumps(md5input, ensure_ascii=False, sort_keys=True) + '\n'
    return json_str, hashlib.md5(json_str.encode("utf-8")).hexdigest()

def count_dim_sizes(ds, ds_element_index, ndims):
    tf.debugging.assert_greater(ndims, 0)
    get_shape_at_index = lambda *t: tf.shape(t[ds_element_index])
    shapes_ds = ds.map(get_shape_at_index)
    ones = tf.ones(ndims, dtype=tf.int32)
    shape_indices = tf.range(ndims, dtype=tf.int32)
    max_sizes = shapes_ds.reduce(
        tf.zeros(ndims, dtype=tf.int32),
        lambda acc, shape: tf.math.maximum(acc, shape))
    max_max_size = tf.reduce_max(max_sizes)
    @tf.function
    def accumulate_dim_size_counts(counter, shape):
        enumerated_shape = tf.stack((shape_indices, shape), axis=1)
        return tf.tensor_scatter_nd_add(counter, enumerated_shape, ones)
    size_counts = shapes_ds.reduce(
        tf.zeros((ndims, max_max_size + 1), dtype=tf.int32),
        accumulate_dim_size_counts)
    sorted_size_indices = tf.argsort(size_counts, direction="DESCENDING")
    sorted_size_counts = tf.gather(size_counts, sorted_size_indices, batch_dims=1)
    is_nonzero = sorted_size_counts > 0
    return tf.ragged.stack(
        (tf.ragged.boolean_mask(sorted_size_counts, is_nonzero),
         tf.ragged.boolean_mask(sorted_size_indices, is_nonzero)),
        axis=2)

def now_str(date=False):
    return str(datetime.datetime.now() if date else int(time.time()))


class E2EBase(Command):

    @classmethod
    def create_argparser(cls, parent_parser):
        parser = super().create_argparser(parent_parser)
        optional = parser.add_argument_group("options")
        optional.add_argument("--file-limit",
            type=int,
            help="Extract only up to this many files from the wavpath list (e.g. for debugging).")
        return parser

    def get_checkpoint_dir(self):
        model_cache_dir = os.path.join(self.cache_dir, self.model_id)
        return os.path.join(model_cache_dir, "checkpoints")

    def create_model(self, config, skip_training=False):
        model_cache_dir = os.path.join(self.cache_dir, self.model_id)
        tensorboard_log_dir = os.path.join(model_cache_dir, "tensorboard", "logs")
        tensorboard_dir = os.path.join(tensorboard_log_dir, now_str())
        default_tensorboard_config = {
            "log_dir": tensorboard_dir,
            "profile_batch": 0,
            "histogram_freq": 1,
        }
        tensorboard_config = dict(default_tensorboard_config, **config.get("tensorboard", {}))
        checkpoint_dir = self.get_checkpoint_dir()
        checkpoint_format = "epoch{epoch:06d}.hdf5"
        if "checkpoints" in config and "format" in config["checkpoints"]:
            checkpoint_format = config["checkpoints"].pop("format")
        default_checkpoints_config = {
            "filepath": os.path.join(checkpoint_dir, checkpoint_format),
        }
        checkpoints_config = dict(default_checkpoints_config, **config.get("checkpoints", {}))
        callbacks_kwargs = {
            "checkpoints": checkpoints_config,
            "early_stopping": config.get("early_stopping"),
            "tensorboard": tensorboard_config,
            "other_callbacks": config.get("other_callbacks", []),
        }
        if not skip_training:
            self.make_named_dir(tensorboard_dir, "tensorboard")
            self.make_named_dir(checkpoint_dir, "checkpoints")
        if self.args.verbosity > 1:
            print("KerasWrapper callback parameters will be set to:")
            yaml_pprint(callbacks_kwargs)
            print()
        return models.KerasWrapper(self.model_id, config["model_definition"], **callbacks_kwargs)

    def extract_features(self, datasets, config, datagroup_key, trim_audio, debug_squeeze_last_dim):
        args = self.args
        utt2path = collections.OrderedDict()
        utt2meta = collections.OrderedDict()
        if args.verbosity > 1:
            print("Extracting features from datagroup '{}'".format(datagroup_key))
            if args.verbosity > 2:
                yaml_pprint(config)
        num_utts_dropped = collections.Counter()
        for ds_config in datasets:
            if args.verbosity > 1:
                print("Dataset '{}'".format(ds_config["key"]))
            datagroup = ds_config["datagroups"][datagroup_key]
            utt2path_path = os.path.join(datagroup["path"], datagroup.get("utt2path", "utt2path"))
            utt2label_path = os.path.join(datagroup["path"], datagroup.get("utt2label", "utt2label"))
            if args.verbosity:
                print("Reading labels for utterances from utt2label file '{}'".format(utt2label_path))
            if args.verbosity > 1:
                print("Expected labels (utterances with other labels will be ignored):")
                for l in ds_config["labels"]:
                    print(l)
            enabled_labels = set(ds_config["labels"])
            skipped_utterances = set()
            for utt, label, *rest in parse_space_separated(utt2label_path):
                if label not in enabled_labels:
                    skipped_utterances.add(utt)
                    continue
                assert utt not in utt2meta, "duplicate utterance id found when parsing labels: '{}'".format(utt)
                utt2meta[utt] = {"label": label, "dataset": ds_config["key"], "duration_sec": -1.0}
            utt2dur_path = os.path.join(datagroup["path"], datagroup.get("utt2dur", "utt2dur"))
            if os.path.exists(utt2dur_path):
                if args.verbosity:
                    print("Reading durations from utt2dur file '{}'".format(utt2dur_path))
                for utt, duration, *rest in parse_space_separated(utt2dur_path):
                    assert utt in utt2meta, "utterance id without label found when parsing durations: '{}'".format(utt)
                    utt2meta[utt]["duration_sec"] = float(duration)
            else:
                if args.verbosity:
                    print("Skipping signal duration parse since utt2dur file '{}' does not exist".format(utt2dur_path))
            if args.verbosity:
                print("Reading paths of wav files from utt2path file '{}'".format(utt2path_path))
            for utt, path, *rest in parse_space_separated(utt2path_path):
                if utt in skipped_utterances:
                    continue
                assert utt not in utt2path, "duplicate utterance id found when parsing paths: '{}'".format(utt)
                utt2path[utt] = path
        if args.verbosity > 1:
            print("Total amount of non-empty lines read from utt2path {}, and utt2meta {}".format(len(utt2path), len(utt2meta)))
            print("Total amount of utterances skipped due to unexpected labels: {}".format(len(skipped_utterances)))
        # All utterance ids must be present in both files
        assert set(utt2path) == set(utt2meta), "Mismatching sets of utterances in utt2path and utt2meta, the utterance ids must be exactly the same"
        utterance_list = list(utt2path.keys())
        if args.shuffle_utt2path or datagroup.get("shuffle_utt2path", False):
            if args.verbosity > 1:
                print("Shuffling utterance ids, all wavpaths in the utt2path list will be processed in random order.")
            random.shuffle(utterance_list)
        else:
            if args.verbosity > 1:
                print("Not shuffling utterance ids, all wavs will be processed in order of the utt2path list.")
        if args.file_limit:
            if args.verbosity > 1:
                print("--file-limit set at {0}, using at most {0} utterances from the utterance id list, starting at the beginning of utt2path".format(args.file_limit))
            utterance_list = utterance_list[:args.file_limit]
            if args.verbosity > 3:
                print("Using utterance ids:")
                yaml_pprint(utterance_list)
        paths = []
        paths_meta = []
        for utt in utterance_list:
            paths.append(utt2path[utt])
            meta = utt2meta[utt]
            paths_meta.append((utt, meta["label"], meta["dataset"], meta["duration_sec"]))
        if args.verbosity:
            print("Starting feature extraction for datagroup '{}' from {} files".format(datagroup_key, len(paths)))
            if args.verbosity > 3:
                print("All utterances:")
                for path, (utt, label, dataset, *rest) in zip(paths, paths_meta):
                    print(utt, label, dataset, sep='\t')
        if config["type"] == "sparsespeech":
            seg2utt_path = os.path.join(datagroup["path"], "segmented", datagroup.get("seg2utt", "seg2utt"))
            if args.verbosity:
                print("Parsing SparseSpeech features")
                print("Reading utterance segmentation data from seg2utt file '{}'".format(seg2utt_path))
            seg2utt = collections.OrderedDict(
                row[:2] for row in parse_space_separated(seg2utt_path)
            )
            enc_path = config["sparsespeech_paths"]["output"][datagroup_key]
            feat_path = config["sparsespeech_paths"]["input"][datagroup_key]
            if args.verbosity:
                print("SparseSpeech input: '{}' and encoding: '{}'".format(feat_path, enc_path))
            feat = tf_data.parse_sparsespeech_features(config, enc_path, feat_path, seg2utt, utt2label)
        elif config["type"] == "kaldi":
            feat_conf = dict(config["datagroups"][datagroup_key])
            kaldi_feats_scp = feat_conf.pop("features_path")
            expected_shape = feat_conf.pop("shape")
            if args.verbosity:
                print("Parsing Kaldi features from '{}' with expected shape {}".format(kaldi_feats_scp, expected_shape))
            feat = tf_data.parse_kaldi_features(utterance_list, kaldi_feats_scp, utt2label, expected_shape, feat_conf)
        else:
            feat = tf_data.extract_features_from_paths(
                config,
                paths,
                paths_meta,
                datagroup_key,
                trim_audio=trim_audio,
                debug_squeeze_last_dim=debug_squeeze_last_dim,
                verbosity=args.verbosity,
            )
        return feat


class Train(E2EBase):

    @classmethod
    def create_argparser(cls, parent_parser):
        parser = super().create_argparser(parent_parser)
        optional = parser.add_argument_group("options")
        optional.add_argument("--skip-training",
            action="store_true",
            default=False)
        optional.add_argument("--debug-dataset",
            action="store_true",
            default=False)
        optional.add_argument("--shuffle-utt2path",
            action="store_true",
            default=False,
            help="Override utt2path shuffling")
        optional.add_argument("--exhaust-dataset-iterator",
            action="store_true",
            default=False,
            help="Explictly iterate once over the feature extractor tf.data.Dataset object in order to evaluate the feature extraction pipeline and fill the feature cache on disk. Using this with --skip-training allows you to extract features on multiple CPUs without needing a GPU.")
        optional.add_argument("--dataset-config",
            type=str,
            action=ExpandAbspath,
            help="Path to a yaml-file containing a list of datasets.")
        return parser

    def train(self):
        args = self.args
        if args.verbosity:
            print("Preparing model for training")
        training_config = self.experiment_config["experiment"]
        feat_config = self.experiment_config["features"]
        if args.verbosity > 1:
            print("Using model parameters:")
            yaml_pprint(training_config)
            print()
        if args.verbosity > 1:
            print("Using feature extraction parameters:")
            yaml_pprint(feat_config)
            print()
        if args.dataset_config:
            assert "dataset" not in self.experiment_config, "config file should not contain a 'dataset' key if a separate datasets yaml is supplied"
            dataset_config = system.load_yaml(args.dataset_config)
            self.experiment_config["datasets"] = [d for d in dataset_config if d["key"] in self.experiment_config["datasets"]]
            labels = sorted(set(l for d in self.experiment_config["datasets"] for l in d["labels"]))
        else:
            labels = self.experiment_config["dataset"]["labels"]
        label2int, OH = make_label2onehot(labels)
        onehot_dims = self.experiment_config["experiment"].get("onehot_dims")
        if onehot_dims:
            def label2onehot(label):
                o = OH[label2int.lookup(label)]
                return tf.concat((o, tf.zeros(onehot_dims - tf.size(o))))
        else:
            def label2onehot(label):
                return OH[label2int.lookup(label)]
        if args.verbosity > 2:
            print("Generating onehot encoding from labels:", ', '.join(labels))
            print("Generated onehot encoding as tensors:")
            for l in labels:
                l = tf.constant(l, dtype=tf.string)
                tf_data.tf_print(l, "\t", label2onehot(l))
        self.model_id = training_config["name"]
        model = self.create_model(dict(training_config), args.skip_training)
        if args.verbosity > 1:
            print("Preparing model")
        model.prepare(labels, training_config)
        if args.verbosity:
            print("Using model:\n{}".format(str(model)))
        dataset = {}
        for ds in ("train", "validation"):
            if args.verbosity > 2:
                print("Dataset config for '{}'".format(ds))
                yaml_pprint(training_config[ds])
            ds_config = dict(training_config, **training_config[ds])
            del ds_config["train"], ds_config["validation"]
            summary_kwargs = dict(ds_config.get("dataset_logger", {}))
            debug_squeeze_last_dim = ds_config["input_shape"][-1] == 1
            datagroup_key = ds_config.pop("datagroup")
            conf_json, conf_checksum = config_checksum(self.experiment_config, datagroup_key)
            extractor_ds = self.extract_features(
                self.experiment_config["datasets"],
                json.loads(json.dumps(feat_config)),
                datagroup_key,
                summary_kwargs.pop("trim_audio", False),
                debug_squeeze_last_dim,
            )
            if ds_config.get("persistent_features_cache", True):
                features_cache_dir = os.path.join(self.cache_dir, "features")
            else:
                features_cache_dir = "/tmp/tensorflow-cache"
            features_cache_path = os.path.join(
                features_cache_dir,
                datagroup_key,
                feat_config["type"],
                conf_checksum,
            )
            self.make_named_dir(os.path.dirname(features_cache_path), "features cache")
            if not os.path.exists(features_cache_path + ".md5sum-input"):
                with open(features_cache_path + ".md5sum-input", "w") as f:
                    print(conf_json, file=f, end='')
                if args.verbosity:
                    print("Writing features into new cache: '{}'".format(features_cache_path))
            else:
                if args.verbosity:
                    print("Loading features from existing cache: '{}'".format(features_cache_path))
            extractor_ds = extractor_ds.cache(filename=features_cache_path)
            if args.exhaust_dataset_iterator:
                if args.verbosity:
                    print("--exhaust-dataset-iterator given, now iterating once over the dataset iterator to fill the features cache.")
                # This forces the extractor_ds pipeline to be evaluated, and the features being serialized into the cache
                i = 0
                if args.verbosity > 1:
                    print(now_str(date=True), "- 0 samples done")
                for i, (feats, *meta) in enumerate(extractor_ds.as_numpy_iterator(), start=1):
                    if args.verbosity > 1 and i % 10000 == 0:
                        print(now_str(date=True), "-", i, "samples done")
                    if args.verbosity > 3:
                        tf_data.tf_print("sample:", i, "features shape:", tf.shape(feats), "metadata:", *meta)
                if args.verbosity > 1:
                    print(now_str(date=True), "- all", i, "samples done")
            dataset[ds] = tf_data.prepare_dataset_for_training(
                extractor_ds,
                ds_config,
                feat_config,
                label2onehot,
                self.model_id,
                conf_checksum=conf_checksum,
                verbosity=args.verbosity,
            )
            if args.debug_dataset:
                if args.verbosity:
                    print("--debug-dataset given, iterating over the dataset to gather stats")
                if args.verbosity > 1:
                    print("Counting all unique dim sizes of elements at index 0 in dataset")
                for axis, size_counts in enumerate(count_dim_sizes(dataset[ds], 0, len(ds_config["input_shape"]) + 1)):
                    print("axis {}\n[count size]:".format(axis))
                    tf_data.tf_print(size_counts, summarize=10)
                if summary_kwargs:
                    logdir = os.path.join(os.path.dirname(model.tensorboard.log_dir), "dataset", ds)
                    if os.path.isdir(logdir):
                        if args.verbosity:
                            print("summary_kwargs available, but '{}' already exists, not iterating over dataset again".format(logdir))
                    else:
                        if args.verbosity:
                            print("Datagroup '{}' has a dataset logger defined. We will iterate over {} batches of samples from the dataset to create TensorBoard summaries of the input data into '{}'.".format(ds, summary_kwargs.get("num_batches", "'all'"), logdir))
                        self.make_named_dir(logdir)
                        writer = tf.summary.create_file_writer(logdir)
                        summary_kwargs["debug_squeeze_last_dim"] = debug_squeeze_last_dim
                        with writer.as_default():
                            logged_dataset = tf_data.attach_dataset_logger(dataset[ds], feat_config["type"], **summary_kwargs)
                            if args.verbosity:
                                print("Dataset logger attached to '{0}' dataset iterator, now exhausting the '{0}' dataset logger iterator once to write TensorBoard summaries of model input data".format(ds))
                            i = 0
                            max_outputs = summary_kwargs.get("max_outputs", 10)
                            for i, (samples, labels, *meta) in enumerate(logged_dataset.as_numpy_iterator()):
                                if args.verbosity > 1 and i % (2000//ds_config.get("batch_size", 1)) == 0:
                                    print(i, "batches done")
                                if args.verbosity > 3:
                                    tf_data.tf_print(
                                            "batch:", i,
                                            "utts", meta[0][:max_outputs],
                                            "samples shape:", tf.shape(samples),
                                            "onehot shape:", tf.shape(labels),
                                            "wav.audio.shape", meta[1].audio.shape,
                                            "wav.sample_rate[0]", meta[1].sample_rate[0])
                            if args.verbosity > 1:
                                print(i, "batches done")
                            del logged_dataset
        checkpoint_dir = self.get_checkpoint_dir()
        checkpoints = [c.name for c in os.scandir(checkpoint_dir) if c.is_file()] if os.path.isdir(checkpoint_dir) else []
        if checkpoints:
            if "checkpoints" in training_config:
                monitor_value = training_config["checkpoints"]["monitor"]
                monitor_mode = training_config["checkpoints"].get("mode")
            else:
                monitor_value = "epoch"
                monitor_mode = None
            checkpoint_path = os.path.join(checkpoint_dir, models.get_best_checkpoint(checkpoints, key=monitor_value, mode=monitor_mode))
            if args.verbosity:
                print("Loading model weights from checkpoint file '{}' according to monitor value '{}'".format(checkpoint_path, monitor_value))
            model.load_weights(checkpoint_path)
        if args.verbosity:
            print("\nStarting training")
        if args.skip_training:
            print("--skip-training given, will not call model.fit")
            return
        history = model.fit(dataset["train"], dataset["validation"], training_config)
        if args.verbosity:
            print("\nTraining finished after {} epochs at epoch {}".format(len(history.epoch), history.epoch[-1] + 1))
            print("metric:\tmin (epoch),\tmax (epoch):")
            for name, epoch_vals in history.history.items():
                vals = np.array(epoch_vals)
                print("{}:\t{:.6f} ({:d}),\t{:.6f} ({:d})".format(
                    name,
                    vals.min(),
                    vals.argmin() + 1,
                    vals.max(),
                    vals.argmax() + 1
                ))
        history_cache_dir = os.path.join(self.cache_dir, self.model_id, "history")
        now_s = now_str()
        for name, epoch_vals in history.history.items():
            history_file = os.path.join(history_cache_dir, now_s, name)
            self.make_named_dir(os.path.dirname(history_file), "training history")
            with open(history_file, "w") as f:
                for epoch, val in enumerate(epoch_vals, start=1):
                    print(epoch, val, file=f)
            if args.verbosity > 1:
                print("wrote history file '{}'".format(history_file))

    def run(self):
        super().run()
        return self.train()


class Predict(E2EBase):
    """
    Use a trained model to produce likelihoods for all target languages from all utterances in the test set.
    Writes all predictions as scores and information about the target and non-target languages into the cache dir.
    """

    @classmethod
    def create_argparser(cls, parent_parser):
        parser = super().create_argparser(parent_parser)
        optional = parser.add_argument_group("predict options")
        optional.add_argument("--score-precision", type=int, default=6)
        optional.add_argument("--score-separator", type=str, default=' ')
        optional.add_argument("--trials", type=str)
        optional.add_argument("--scores", type=str)
        optional.add_argument("--checkpoint",
            type=str,
            help="Specify which Keras checkpoint to load model weights from, instead of using the most recent one.")
        return parser

    def predict(self):
        args = self.args
        if args.verbosity:
            print("Preparing model for prediction")
        self.model_id = self.experiment_config["experiment"]["name"]
        if not args.trials:
            args.trials = os.path.join(self.cache_dir, self.model_id, "predictions", "trials")
        if not args.scores:
            args.scores = os.path.join(self.cache_dir, self.model_id, "predictions", "scores")
        self.make_named_dir(os.path.dirname(args.trials))
        self.make_named_dir(os.path.dirname(args.scores))
        training_config = self.experiment_config["experiment"]
        feat_config = self.experiment_config["features"]
        if args.verbosity > 1:
            print("Using model parameters:")
            yaml_pprint(training_config)
            print()
        if args.verbosity > 1:
            print("Using feature extraction parameters:")
            yaml_pprint(feat_config)
            print()
        model = self.create_model(dict(training_config), skip_training=True)
        if args.verbosity > 1:
            print("Preparing model")
        labels = self.experiment_config["dataset"]["labels"]
        model.prepare(labels, training_config)
        checkpoint_dir = self.get_checkpoint_dir()
        if args.checkpoint:
            checkpoint_path = os.path.join(checkpoint_dir, args.checkpoint)
        elif "best_checkpoint" in self.experiment_config.get("prediction", {}):
            checkpoint_path = os.path.join(checkpoint_dir, self.experiment_config["prediction"]["best_checkpoint"])
        else:
            checkpoints = os.listdir(checkpoint_dir) if os.path.isdir(checkpoint_dir) else []
            if not checkpoints:
                print("Error: Cannot evaluate with a model that has no checkpoints, i.e. is not trained.")
                return 1
            if "checkpoints" in training_config:
                monitor_value = training_config["checkpoints"]["monitor"]
                monitor_mode = training_config["checkpoints"].get("mode")
            else:
                monitor_value = "epoch"
                monitor_mode = None
            checkpoint_path = os.path.join(checkpoint_dir, models.get_best_checkpoint(checkpoints, key=monitor_value, mode=monitor_mode))
        if args.verbosity:
            print("Loading model weights from checkpoint file '{}'".format(checkpoint_path))
        model.load_weights(checkpoint_path)
        if args.verbosity:
            print("\nEvaluating testset with model:")
            print(str(model))
            print()
        ds = "test"
        if args.verbosity > 2:
            print("Dataset config for '{}'".format(ds))
            yaml_pprint(training_config[ds])
        ds_config = dict(training_config, **training_config[ds])
        del ds_config["train"], ds_config["validation"]
        if args.verbosity and "dataset_logger" in ds_config:
            print("Warning: dataset_logger in the test datagroup has no effect.")
        datagroup_key = ds_config.pop("datagroup")
        datagroup = self.experiment_config["dataset"]["datagroups"][datagroup_key]
        utt2path_path = os.path.join(datagroup["path"], datagroup.get("utt2path", "utt2path"))
        utt2label_path = os.path.join(datagroup["path"], datagroup.get("utt2label", "utt2label"))
        utt2path = collections.OrderedDict(
            row[:2] for row in parse_space_separated(utt2path_path)
        )
        utt2label = collections.OrderedDict(
            row[:2] for row in parse_space_separated(utt2label_path)
        )
        utterance_list = list(utt2path.keys())
        if args.file_limit:
            utterance_list = utterance_list[:args.file_limit]
            if args.verbosity > 3:
                print("Using utterance ids:")
                yaml_pprint(utterance_list)
        int2label = self.experiment_config["dataset"]["labels"]
        label2int, OH = make_label2onehot(int2label)
        onehot_dims = self.experiment_config["experiment"].get("onehot_dims")
        if onehot_dims:
            def label2onehot(label):
                o = OH[label2int.lookup(label)]
                return tf.concat((o, tf.zeros(onehot_dims - tf.size(o))))
        else:
            def label2onehot(label):
                return OH[label2int.lookup(label)]
        labels_set = set(int2label)
        paths = []
        paths_meta = []
        for utt in utterance_list:
            label = utt2label[utt]
            if label not in labels_set:
                continue
            paths.append(utt2path[utt])
            paths_meta.append((utt, label))
        if args.verbosity:
            print("Extracting test set features for prediction")
        features = self.extract_features(
            feat_config,
            "test",
            trim_audio=False,
            debug_squeeze_last_dim=(ds_config["input_shape"][-1] == 1),
        )
        conf_json, conf_checksum = config_checksum(self.experiment_config, datagroup_key)
        features = tf_data.prepare_dataset_for_training(
            features,
            ds_config,
            feat_config,
            label2onehot,
            self.model_id,
            verbosity=args.verbosity,
            conf_checksum=conf_checksum,
        )
        # drop meta wavs required only for vad
        features = features.map(lambda *t: t[:3])
        if ds_config.get("persistent_features_cache", True):
            features_cache_dir = os.path.join(self.cache_dir, "features")
        else:
            features_cache_dir = "/tmp/tensorflow-cache"
        features_cache_path = os.path.join(
            features_cache_dir,
            self.experiment_config["dataset"]["key"],
            ds,
            feat_config["type"],
            conf_checksum,
        )
        self.make_named_dir(os.path.dirname(features_cache_path), "features cache")
        if not os.path.exists(features_cache_path + ".md5sum-input"):
            with open(features_cache_path + ".md5sum-input", "w") as f:
                print(conf_json, file=f, end='')
            if args.verbosity:
                print("Writing features into new cache: '{}'".format(features_cache_path))
        else:
            if args.verbosity:
                print("Loading features from existing cache: '{}'".format(features_cache_path))
        features = features.cache(filename=features_cache_path)
        if args.verbosity:
            print("Gathering all utterance ids from features dataset iterator")
        # Gather utterance ids, this also causes the extraction pipeline to be evaluated
        utterance_ids = []
        i = 0
        if args.verbosity > 1:
            print(now_str(date=True), "- 0 samples done")
        for _, _, uttids in features.as_numpy_iterator():
            for uttid in uttids:
                utterance_ids.append(uttid.decode("utf-8"))
                i += 1
                if args.verbosity > 1 and i % 10000 == 0:
                    print(now_str(date=True), "-", i, "samples done")
        if args.verbosity > 1:
            print(now_str(date=True), "- all", i, "samples done")
        if args.verbosity:
            print("Features extracted, writing target and non-target language information for each utterance to '{}'.".format(args.trials))
        with open(args.trials, "w") as trials_f:
            for utt, target in utt2label.items():
                for lang in int2label:
                    print(lang, utt, "target" if target == lang else "nontarget", file=trials_f)
        if args.verbosity:
            print("Starting prediction with model")
        predictions = model.predict(features.map(lambda *t: t[0]))
        if args.verbosity > 1:
            print("Done predicting, model returned predictions of shape {}. Writing them to '{}'.".format(predictions.shape, args.scores))
        num_predictions = 0
        with open(args.scores, "w") as scores_f:
            print(*int2label, file=scores_f)
            for utt, pred in zip(utterance_ids, predictions):
                pred_scores = [np.format_float_positional(x, precision=args.score_precision) for x in pred]
                print(utt, *pred_scores, sep=args.score_separator, file=scores_f)
                num_predictions += 1
        if args.verbosity:
            print("Wrote {} prediction scores to '{}'.".format(num_predictions, args.scores))

    def run(self):
        super().run()
        return self.predict()


#class Evaluate(E2EBase):
#    """Evaluate predicted scores by average detection cost (C_avg)."""

#    @classmethod
#    def create_argparser(cls, parent_parser):
#        parser = super().create_argparser(parent_parser)
#        optional = parser.add_argument_group("evaluate options")
#        optional.add_argument("--trials", type=str)
#        optional.add_argument("--scores", type=str)
#        optional.add_argument("--threshold-bins", type=int, default=40)
#        optional.add_argument("--convert-scores", choices=("softmax", "exp", "none"), default=None)
#        return parser

#    #TODO tf is very slow in the for loop, maybe numpy would be sufficient
#    def evaluate(self):
#        args = self.args
#        self.model_id = self.experiment_config["experiment"]["name"]
#        if not args.trials:
#            args.trials = os.path.join(self.cache_dir, self.model_id, "predictions", "trials")
#        if not args.scores:
#            args.scores = os.path.join(self.cache_dir, self.model_id, "predictions", "scores")
#        if args.verbosity > 1:
#            print("Evaluating minimum average detection cost using trials '{}' and scores '{}'".format(args.trials, args.scores))
#        score_lines = list(parse_space_separated(args.scores))
#        langs = score_lines[0]
#        lang2int = {l: i for i, l in enumerate(langs)}
#        utt2scores = {utt: tf.constant([float(s) for s in scores], dtype=tf.float32) for utt, *scores in score_lines[1:]}
#        if args.verbosity > 1:
#            print("Parsed scores for {} utterances".format(len(utt2scores)))
#        if args.convert_scores == "softmax":
#            print("Applying softmax on logit scores")
#            utt2scores = {utt: tf.keras.activations.softmax(tf.expand_dims(scores, 0))[0] for utt, scores in utt2scores.items()}
#        elif args.convert_scores == "exp":
#            print("Applying exp on log likelihood scores")
#            utt2scores = {utt: tf.math.exp(scores) for utt, scores in utt2scores.items()}
#        if args.verbosity > 2:
#            print("Asserting all scores sum to 1")
#            tolerance = 1e-3
#            for utt, scores in utt2scores.items():
#                one = tf.constant(1.0, dtype=tf.float32)
#                tf.debugging.assert_near(
#                    tf.reduce_sum(scores),
#                    one,
#                    rtol=tolerance,
#                    atol=tolerance,
#                    message="failed to convert log likelihoods to probabilities, the probabilities of predictions for utterance '{}' does not sum to 1".format(utt))
#        if args.verbosity > 1:
#            print("Generating {} threshold bins".format(args.threshold_bins))
#        assert args.threshold_bins > 0
#        from_logits = False
#        max_score = tf.constant(-float("inf"), dtype=tf.float32)
#        min_score = tf.constant(float("inf"), dtype=tf.float32)
#        for utt, scores in utt2scores.items():
#            max_score = tf.math.maximum(max_score, tf.math.reduce_max(scores))
#            min_score = tf.math.minimum(min_score, tf.math.reduce_min(scores))
#        if args.verbosity > 2:
#            tf_data.tf_print("Max score", max_score, "min score", min_score)
#        thresholds = tf.linspace(min_score, max_score, args.threshold_bins)
#        if args.verbosity > 2:
#            print("Score thresholds for language detection decisions:")
#            tf_data.tf_print(thresholds, summarize=5)
#        # First do C_avg to get the best threshold
#        cavg = AverageDetectionCost(langs, theta_det=list(thresholds.numpy()))
#        if args.verbosity > 1:
#            print("Sorting trials")
#        trials_by_utt = sorted(parse_space_separated(args.trials), key=lambda t: t[1])
#        trials_by_utt = [
#            (utt, tf.constant([float(t == "target") for _, _, t in sorted(group, key=lambda t: lang2int[t[0]])], dtype=tf.float32))
#            for utt, group in itertools.groupby(trials_by_utt, key=lambda t: t[1])
#        ]
#        if args.verbosity:
#            print("Computing minimum C_avg using {} score thresholds".format(len(thresholds)))
#        # Collect labels and predictions for confusion matrix
#        cm_labels = []
#        cm_predictions = []
#        for utt, y_true in trials_by_utt:
#            if utt not in utt2scores:
#                print("Warning: correct class for utterance '{}' listed in trials but it has no predicted scores, skipping".format(utt), file=sys.stderr)
#                continue
#            y_pred = utt2scores[utt]
#            # Update using singleton batches
#            cavg.update_state(
#                tf.expand_dims(y_true, 0),
#                tf.expand_dims(y_pred, 0)
#            )
#            cm_labels.append(tf.math.argmax(y_true))
#            cm_predictions.append(tf.math.argmax(y_pred))
#        # Evaluating the cavg result has a side effect of generating the argmin of the minimum cavg into cavg.min_index
#        _ = cavg.result()
#        min_threshold = thresholds[cavg.min_index].numpy()
#        def print_metric(m):
#            print("{:15s}\t{:.3f}".format(m.name + ":", m.result().numpy()))
#        print("min C_avg at threshold {:.6f}".format(min_threshold))
#        print_metric(cavg)
#        # Now we know the threshold that minimizes C_avg and use the same threshold to compute all other metrics
#        metrics = [
#            M(langs, from_logits=from_logits, thresholds=min_threshold)
#            for M in (AverageEqualErrorRate, AveragePrecision, AverageRecall)
#        ]
#        if args.verbosity:
#            print("Computing rest of the metrics using threshold {:.6f}".format(min_threshold))
#        for utt, y_true in trials_by_utt:
#            if utt not in utt2scores:
#                continue
#            y_true_batch = tf.expand_dims(y_true, 0)
#            y_pred = tf.expand_dims(utt2scores[utt], 0)
#            for m in metrics:
#                m.update_state(y_true_batch, y_pred)
#        for avg_m in metrics:
#            print_metric(avg_m)
#        print("\nMetrics by target, using threshold {:.6f}".format(min_threshold))
#        for avg_m in metrics:
#            print(avg_m.name)
#            for m in avg_m:
#                print_metric(m)
#        print("\nConfusion matrix")
#        cm_labels = tf.cast(tf.stack(cm_labels), dtype=tf.int32)
#        cm_predictions = tf.cast(tf.stack(cm_predictions), dtype=tf.int32)
#        confusion_matrix = tf.math.confusion_matrix(cm_labels, cm_predictions, len(langs))
#        print(langs)
#        print(np.array_str(confusion_matrix.numpy()))

#    def run(self):
#        super().run()
#        return self.evaluate()


class Util(E2EBase):
    tasks = (
        "get_cache_checksum",
    )

    @classmethod
    def create_argparser(cls, subparsers):
        parser = super().create_argparser(subparsers)
        optional = parser.add_argument_group("util options")
        optional.add_argument("--get-cache-checksum",
            type=str,
            metavar="datagroup_key",
            help="For a given datagroup key, compute md5sum of config file in the same way as it would be computed when generating the filename for the features cache. E.g. for checking if the pipeline will be using the cache or start the feature extraction from scratch.")
        return parser

    def get_cache_checksum(self):
        datagroup_key = self.args.get_cache_checksum
        conf_json, conf_checksum = config_checksum(self.experiment_config, datagroup_key)
        if self.args.verbosity:
            print(10*'-' + " md5sum input begin " + 10*'-')
            print(conf_json, end='')
            print(10*'-' + "  md5sum input end  " + 10*'-')
        print("cache md5 checksum for datagroup key '{}' is:".format(datagroup_key))
        print(conf_checksum)

    def run(self):
        super().run()
        return self.run_tasks()


command_tree = [
    (E2E, [Train, Predict, Util]),
]
