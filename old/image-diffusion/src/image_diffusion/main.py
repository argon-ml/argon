from argon.core.dataclasses import dataclass
from argon.core import tree
from argon.random import PRNGSequence
from argon.diffusion.ddpm import DDPMSchedule
from argon.train.reporting import Image
from argon.util.registry import Registry

from argon.datasets.common import Dataset
from argon.datasets.vision import LabeledImage
from argon.data.normalizer import Normalizer
from argon.data import Data, PyTreeData

from pathlib import Path

import argon.data.normalizer as normalizers
import argon.graphics
import argon.train
import argon.train.wandb
import argon.train.console
import argon.random
import argon.store
import argon.datasets.vision
import argon.models

import argon.numpy as jnp
import argon.core as F

import itertools
import numpy as np
import optax
import wandb
import functools

import os
import boto3
import urllib
import tempfile
import jax

from ott.geometry import pointcloud
from ott.problems.linear import linear_problem
from ott.solvers.linear import sinkhorn

import logging
logger = logging.getLogger("image_diffusion")

@dataclass
class Config:
    seed: int = 42
    batch_size: int = 128
    model: str = "diffusion/unet/small"

    dataset: str = "cifar10"
    normalizer: str = "hypercube"

    bucket_url: str = "s3://wandb-data"

    epochs: int | None = None
    iterations: int | None = None

    test_interval: int = 1000
    checkpoint_interval : int | None = 1000

    lr: float = 1e-4
    weight_decay: float = 1e-5

    timesteps: int = 32

    condition_type: str = "class" # one of "class," "tsne," "image" or "none"
    prediction_type: str = "epsilon"

    target_clip: float | None = None

    log_ot_distance: bool = False
    log_ot_interval: int = 500
    log_ot_batch_size: int = 2048

@dataclass
class ModelConfig:
    model: str
    condition_type: str
    image_shape: tuple[int]
    num_classes: int | None

    def create(self, rng : jax.Array | None = None):
        models = Registry()
        argon.models.register_all(models)
        model = models.create(
            self.model, 
            num_classes=self.num_classes,
            out_channels=self.image_shape[-1]
        )
        if rng is None:
            return model
        pixels_sample = jnp.zeros(self.image_shape, jnp.float32)
        if self.condition_type == "class":
            cond_sample = jnp.zeros((), dtype=jnp.uint32)
        elif self.condition_type == "image":
            cond_sample = jnp.zeros(self.image_shape, jnp.float32)
        elif self.condition_type == "tsne":
            cond_sample = jnp.zeros((2,), jnp.float32)
        elif self.condition_type == "none":
            cond_sample = None

        vars = F.jit(model.init)(
            rng, pixels_sample,  jnp.zeros((), dtype=jnp.uint32), 
            cond=cond_sample
        )
        return model, vars

@dataclass
class Sample:
    data: F.Array
    cond: F.Array
    label: F.Array

@dataclass
class Checkpoint:
    dataset: str
    normalizer: str
    config: ModelConfig
    schedule: DDPMSchedule
    vars: dict
    opt_state: dict

    def create_data(self):
        datasets = Registry()
        argon.datasets.vision.register_all(datasets)
        dataset : Dataset[LabeledImage] = datasets.create(self.dataset)
        norm, train_data, test_data = preprocess_dataset(self.dataset, self.config.condition_type, self.normalizer, dataset)
        return norm, train_data, test_data

def preprocess_dataset(dataset_name: str, condition_type: str, normalizer_name: str, dataset: Dataset[LabeledImage]) -> tuple[Data[Sample], Data[Sample]]:
    train_data, test_data = dataset.split("train"), dataset.split("test")
    normalizer = dataset.normalizer(normalizer_name)
    image_normalizer = normalizer.map(lambda x: x.pixels)
    orig_label_normalizer = normalizer.map(lambda x: x.label)
    if condition_type == "tsne":
        train_data = train_data.as_pytree()
        test_data = test_data.as_pytree()

        # Fit a TSNE-embedding to the dataset
        import argon.util.tsne as tsne
        cond = jnp.concatenate([train_data.pixels, test_data.pixels], axis=0)
        cond = jax.vmap(image_normalizer.normalize)(cond)
        if True:
            logger.info("Learning T-SNE embedding...")
            cond = tsne.randomized_tsne(
                cond,
                n_components=2,
                perplexity=40,
                n_iter=4000,
                learning_rate=800,
                rng_key=argon.random.key(42)
            ).embedding
        else:
            path = Path(os.environ["HOME"]) / ".argon_dataset_cache" / "tsne"
            path.mkdir(parents=True, exist_ok=True)
            path = path / f"{dataset_name}_{normalizer_name}.npy"
            if path.exists():
                cond = jnp.load(path)
            else:
                logger.info("Learning T-SNE embedding...")
                from sklearn.manifold import TSNE
                tsne = TSNE(
                    max_iter=500, 
                    perplexity=40, 
                    random_state=42
                )
                cond = tsne.fit_transform(cond.reshape((cond.shape[0], -1)))
                cond = jnp.array(cond)
                logger.info("Fit T-SNE embedding")
                jnp.save(path, cond)

        cond = cond - jnp.mean(cond, axis=0)
        cond = cond/jnp.std(cond, axis=0)

        train_cond = cond[:train_data.pixels.shape[0]]
        test_cond = cond[train_data.pixels.shape[0]:]

        normalizer = normalizers.Compose(
            Sample(
                data=image_normalizer,
                cond=normalizers.Identity(jnp.zeros((2,))),
                label=orig_label_normalizer
            )
        )
        return normalizer, PyTreeData(
            Sample(train_data.pixels, train_cond, train_data.label),
        ), PyTreeData(
            Sample(test_data.pixels, test_cond, test_data.label),
        )
    else:
        def preprocess_data(data : Data[LabeledImage]) -> Data[Sample]:
            if condition_type == "none":
                return data.map(
                    lambda x: Sample(x.pixels, None, None)
                )
            elif condition_type == "class":
                assert hasattr(data.structure, "label")
                return data.map(
                    lambda x: Sample(x.pixels, x.label, x.label)
                )
            elif condition_type == "tsne":
                return data
            elif condition_type == "image_other":
                # make index pairs for matching classes
                labels = data.map(lambda x: x.label).as_pytree()
                images = data.map(lambda x: x.pixels).as_pytree()
                classes = jnp.max(labels) + 1

                class_indices = [
                    jnp.argwhere(labels == i)[...,0] for i in range(classes)
                ]
                per_class_max = min([len(x) for x in class_indices])
                class_indices = [x[:per_class_max] for x in class_indices]
                # a list of the pixel images for each class, balanced 
                class_images = [images[i] for i in class_indices]
                assert len(class_images) % 2 == 0
                class_pairs = tree.map(
                    lambda *class_pairs: jnp.concatenate(class_pairs, axis=0),
                    # pair each class with the next class, (wrapping around)
                    *zip(class_images[::2], class_images[1::2]),
                    *zip(class_images[1::2], class_images[2::2] + [class_images[0]])
                )
                class_labels = [jnp.full((per_class_max,), label) for label in itertools.chain(
                    range(0, len(class_images), 2),
                    range(1, len(class_images), 2),
                )]
                class_labels = jnp.concatenate(class_labels, axis=0)
                return PyTreeData(Sample(
                    cond=class_pairs[0],
                    data=class_pairs[1],
                    label=class_labels,
                ))
        label_normalizer = (
            orig_label_normalizer 
            if condition_type != "image" else 
            image_normalizer
        )
        normalizer = normalizers.Compose(
            Sample(
                data=image_normalizer,
                cond=label_normalizer,
                label=orig_label_normalizer
            )
        )
        return normalizer, preprocess_data(train_data), preprocess_data(test_data)

def run(config: Config):
    logger.setLevel(logging.DEBUG)
    logger.info(f"Running with config: {config}")
    rng = PRNGSequence(config.seed)
    logger.info(f"Devices: {jax.devices()}")

    wandb_run = wandb.init(
        project="image-diffusion",
        config=tree.flatten_to_dict(config)[0]
    )
    logger.info(f"Logging to {wandb_run.url}")

    datasets = Registry()
    argon.datasets.vision.register_all(datasets)
    dataset : Dataset[LabeledImage] = datasets.create(config.dataset)

    normalizer, train_data, test_data = preprocess_dataset(config.dataset, config.condition_type, config.normalizer, dataset)
    sample = normalizer.normalize(train_data[0])

    classes = len(dataset.classes) if hasattr(dataset, "classes") else None
    model_config = ModelConfig(
        model=config.model,
        condition_type=config.condition_type,
        image_shape=sample.data.shape,
        num_classes=classes
    )
    model, vars = model_config.create(next(rng))

    if config.iterations is not None:
        iterations = config.iterations
    else:
        epochs = config.epochs or 50
        iterations_per_epoch = len(train_data) // config.batch_size
        iterations = iterations_per_epoch * epochs

    logger.info("Creating model...")
    logger.info(f"Parameters: {tree.total_size(vars)}")

    @F.jit
    def diffuser(vars, cond, rng_key, noised_x, t):
        # return schedule.output_from_denoised(noised_x, t, noised_x)
        return model.apply(vars, noised_x, t - 1, cond=cond)

    schedule = DDPMSchedule.make_squaredcos_cap_v2(
        config.timesteps, prediction_type=config.prediction_type,
        clip_sample_range=2.
    )

    lr_schedule = optax.cosine_decay_schedule(
        config.lr, iterations
    )
    # warmup_steps = int(iterations*0.2)
    # lr_schedule = optax.warmup_cosine_decay_schedule(
    #     config.lr/100, config.lr, warmup_steps, iterations
    # )
    optimizer = optax.adamw(lr_schedule, weight_decay=config.weight_decay)
    # optimizer = optax.sgd(lr_schedule)
    optimizer = optax.chain(
        optimizer,
        optax.clip_by_global_norm(1),
    )
    opt_state = optimizer.init(vars["params"])

    @F.jit
    def loss(vars, rng_key, sample):
        s_rng, a_rng = argon.random.split(rng_key)
        sample = normalizer.normalize(sample)
        cond_diffuser = functools.partial(diffuser, vars, sample.cond)
        loss = schedule.loss(
            s_rng, cond_diffuser, sample.data,
        )
        return argon.train.LossOutput(
            loss=loss, metrics={"loss": loss}
        )

    @F.jit
    def generate_samples(vars, labels, rng_key) -> LabeledImage:
        def do_sample(cond_unnormalized, rng_key):
            cond = normalizer.map(lambda x: x.cond).normalize(cond_unnormalized)
            denoiser = functools.partial(diffuser, vars, cond)
            pixels = schedule.sample(rng_key, denoiser, sample.data)
            pixels = normalizer.map(lambda x: x.data).unnormalize(pixels)
            return cond_unnormalized, pixels
        if labels is not None:
            N = tree.axis_size(labels, 0)
        else:
            N = 64
        rngs = argon.random.split(rng_key, N)
        return F.vmap(do_sample)(labels, rngs)

    @F.jit
    def compute_ot_distance(vars, rng_key, sample_batch: Sample):
        sample_batch = F.vmap(normalizer.normalize)(sample_batch)
        cond = sample_batch.cond
        data = sample_batch.data

        ddpm_rng, ddim_rng = argon.random.split(rng_key, 2)
        ddpm_rngs = argon.random.split(ddpm_rng, tree.axis_size(cond))
        ddim_rngs = argon.random.split(ddim_rng, tree.axis_size(cond))
        def sample(rng_key, cond, eta=1.0):
            denoiser = functools.partial(diffuser, vars, cond)
            sampler = lambda rng_key: (cond, schedule.sample(
                        rng_key, denoiser, sample_batch.data[0], eta=eta))
            return F.vmap(sampler)(argon.random.split(rng_key, 3))

        sampled_cond, sampled_data = F.vmap(sample)(ddpm_rngs, cond)
        sampled_cond, sampled_data = tree.map(
                lambda x: jnp.reshape(x, (-1, *x.shape[2:])), 
                (sampled_cond, sampled_data)
        )
        sampled_ddim_cond, sampled_ddim_data = F.vmap(sample)(ddim_rngs, cond)
        sampled_ddim_cond, sampled_ddim_data = tree.map(
                lambda x: jnp.reshape(x, (-1, *x.shape[2:])), 
                (sampled_ddim_cond, sampled_ddim_data)
        )

        def ott_cost(a, b):
            a_flat = jax.vmap(lambda x: tree.ravel_pytree(x)[0])(a)
            b_flat = jax.vmap(lambda x: tree.ravel_pytree(x)[0])(b)
            geom = pointcloud.PointCloud(a_flat, b_flat, epsilon=0.005)
            prob = linear_problem.LinearProblem(geom)
            solver = sinkhorn.Sinkhorn(max_iterations=20_000)
            out = solver(prob)
            return out.primal_cost

        return { "ddpm_gt": ott_cost((sampled_cond, sampled_data), (cond, data)),
                    "ddim_gt": ott_cost((sampled_ddim_cond, sampled_ddim_data), (cond, data)),
                    "ddpm_ddim": ott_cost((sampled_ddim_cond, sampled_ddim_data), (sampled_cond, sampled_data)) }

    batch_loss = argon.train.batch_loss(loss)
    batch_test_loss = argon.train.batch_loss(loss)

    train_stream = train_data.stream().shuffle(next(rng)).batch(config.batch_size)
    test_stream = test_data.stream().batch(2*config.batch_size)
    gen_a_stream = test_data.stream().shuffle(next(rng)).batch(64)
    gen_b_stream = test_data.stream().shuffle(next(rng)).batch(2048)

    ot_stream = test_data.stream().shuffle(next(rng)).batch(config.log_ot_batch_size)

    with argon.train.loop(train_stream, 
            iterations=iterations, rng_key=next(rng)) as loop, \
            test_stream.build() as test_stream, \
            gen_a_stream.build() as gen_a_stream, \
            gen_b_stream.build() as gen_b_stream, \
            ot_stream.build() as ot_stream:
        for epoch in loop.epochs():
            # Log the checkpoint

            for step in epoch.steps():
                train_key, test_key = argon.random.split(step.rng_key)
                opt_state, vars, grad_norm, metrics = argon.train.step(
                    batch_loss, optimizer,
                    opt_state, vars,
                    train_key, step.batch,
                    return_grad_norm=True
                )
                argon.train.wandb.log(
                    step.iteration, metrics, {"lr": lr_schedule(step.iteration), "grad_norm": grad_norm},
                    run=wandb_run, prefix="train/"
                )
                if step.iteration % 100 == 0:
                    argon.train.console.log(
                        step.iteration, metrics, {"lr": lr_schedule(step.iteration)},
                        prefix="train."
                    )
                if config.log_ot_distance and (step.iteration % config.log_ot_interval == 0):
                    test_key, ot_key = argon.random.split(test_key)
                    logger.info("Computing sample OT distance...")
                    if not ot_stream.has_next():
                        ot_stream = ot_stream.reset()
                    ot_stream, ot_batch = ot_stream.next()
                    ot_cost = compute_ot_distance(vars, ot_key, ot_batch)
                    ot_metrics = {"ot_distance": ot_cost}
                    argon.train.console.log(step.iteration, ot_metrics, prefix="test.")
                    argon.train.wandb.log(step.iteration, ot_metrics, run=wandb_run, prefix="test/")

                if step.iteration % config.test_interval == 0:
                    eval_key, sample_key = argon.random.split(test_key)
                    test_stream, test_metrics = argon.train.eval_stream(
                        batch_test_loss, vars, eval_key, test_stream,
                        batches=1
                    )
                    argon.train.wandb.log(
                        step.iteration, test_metrics,
                        run=wandb_run, prefix="test/"
                    )
                    argon.train.console.log(
                        step.iteration, test_metrics, prefix="test."
                    )
                if config.checkpoint_interval and \
                        step.iteration % config.checkpoint_interval == 0 and \
                        config.bucket_url is not None:
                    checkpoint = Checkpoint(
                        dataset=config.dataset,
                        config=model_config,
                        normalizer=config.normalizer,
                        schedule=schedule,
                        vars=vars,
                        opt_state=opt_state
                    )
                    final_result_url = f"{config.bucket_url}/{wandb_run.id}/checkpoint_{epoch.num:04}.zarr.zip"
                    argon.store.save(final_result_url, checkpoint)
                    dataset_sanitized = config.dataset.replace("/", "-")
                    artifact = wandb.Artifact(f"{dataset_sanitized}-ddpm-{step.iteration:06}", type="model", metadata={"step": step.iteration})
                    artifact.add_reference(final_result_url, "checkpoint.zarr.zip")
                    wandb_run.log_artifact(artifact)
                    artifact.wait()
                    logger.info(f"Logged checkpoint: {artifact.source_qualified_name}")
                    del checkpoint
                    del artifact

                    # Log generated images
                    if not gen_a_stream.has_next():
                        gen_a_stream = gen_a_stream.reset()
                    gen_a_stream, gen_batch = gen_a_stream.next()
                    _, gen_samples = generate_samples(vars, gen_batch.cond, sample_key)
                    gen_samples = Image(argon.graphics.image_grid(gen_samples))
                    argon.train.wandb.log(
                        step.iteration, {"samples": gen_samples},
                        run=wandb_run, prefix="generated/"
                    )

        # generate samples and log as a wandb table
        if not gen_b_stream.has_next():
            gen_b_stream = gen_b_stream.reset()
        gen_b_stream, gen_batch = gen_b_stream.next()
        gen_labels, gen_samples = generate_samples(vars, gen_batch.cond, sample_key)
        if config.condition_type == "image":
            label_columns = ["Label"]
            gen_labels = [[wandb.Image(np.array(x))] for x in gen_labels]
        elif config.condition_type == "tsne":
            label_columns = ["Label X", "Label Y"]
            gen_labels = [[i[0], i[1]] for i in gen_labels]
        else:
            label_columns = ["Label"]
            gen_labels = [[dataset.classes[i]] for i in gen_labels]
        gen_table = wandb.Table(
            columns=label_columns + ["Sample"], 
            data=[[*l, wandb.Image(np.array(i))] for l, i in zip(gen_labels, gen_samples)]
        )
        wandb_run.log({"samples": gen_table}, step=step.iteration)
    checkpoint = Checkpoint(
        dataset=config.dataset,
        config=model_config,
        normalizer=config.normalizer,
        schedule=schedule,
        vars=vars,
        opt_state=opt_state
    )
    if config.bucket_url is not None:
        final_result_url = f"{config.bucket_url}/{wandb_run.id}/checkpoint-final.zarr.zip"
        argon.store.save(final_result_url, checkpoint)
        dataset_sanitized = config.dataset.replace("/", "-")
        artifact = wandb.Artifact(f"{dataset_sanitized}-ddpm-final", type="model", metadata={"step": step.iteration})
        artifact.add_reference(final_result_url, "checkpoint.zarr.zip")
        wandb_run.log_artifact(artifact)
        artifact.wait()
        logger.info(f"Artifact: {artifact.source_qualified_name}")
