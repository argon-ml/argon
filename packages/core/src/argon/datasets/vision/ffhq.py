import argon.numpy as jnp
import argon.transforms as agt

from argon.typing import ShapeDtypeStruct
from argon.struct import struct
from argon.data import Data, io, PyTreeData, normalizer as nu
from . import Image, ImageDataset

from argon.datasets.common import DatasetRegistry
from argon.datasets import util

from functools import partial

import jax.image

@struct(frozen=True)
class FFHQData(Data):
    path: str
    start: int
    end: int
    resolution: int = 128

    @agt.jit
    def __getitem__(self, idx) -> Image:
        pixels = io.read_image(
            self.path + "/{:05d}.png", 
            (128, 128, 3),
            idx + self.start
        )
        pixels = jax.image.resize(pixels, 
            (self.resolution, self.resolution, 3), 
            method=jax.image.ResizeMethod.NEAREST)
        return Image(pixels)
    
    def __len__(self):
        return self.end - self.start

@struct(frozen=True)
class FFHQDataset(ImageDataset):
    path: str
    resolution: int

    def split(self, name):
        if name == "train":
            return FFHQData(self.path, 0, 50000, self.resolution)
        elif name == "test":
            return FFHQData(self.path, 50000, 60000, self.resolution)
        elif name == "validation":
            return FFHQData(self.path, 60000, 70000, self.resolution)
    
    def normalizer(self, name):
        norm = nu.ImageNormalizer(
            ShapeDtypeStruct((128, 128, 3), jnp.uint8)
        )
        if name == "hypercube":
            pass
        elif name == "standard_dev":
            data = agt.vmap(norm.normalize)(self.split("train").as_pytree())
            normalizer = nu.StdNormalizer.from_data(PyTreeData(data))
            norm = nu.Chain([norm,
                nu.StdNormalizer(
                    mean=jnp.array([0.5, 0.5, 0.5]),
                    std=jnp.array([0.5, 0.5, 0.5])
                )
            ])
        else:
            raise ValueError(f"Unknown normalizer {name}")
        return nu.Compose(Image(pixels=norm))

def _load_ffhq(quiet=False, resolution=128):
    extract_path = util.cache_path("ffhq") / "images"
    if not extract_path.exists():
        data_path = util.cache_path("ffhq") / "ffhq.zip"
        util.download(data_path,
            gdrive_id="1yKafRWamMCPojb5GBfqIPTgRoNEIISX6",
            md5="cf783275055f3f45a030101b6db4a3be",
            job_name="FFHQ"
        )
        util.extract_to(data_path, extract_path,
            job_name="FFHQ",
            quiet=quiet, strip_folder="thumbnails128x128"
        )
        data_path.unlink()
    return FFHQDataset(path=str(extract_path), resolution=resolution)

def register(registry: DatasetRegistry, prefix=None):
    registry.register("ffhq/128", _load_ffhq, prefix=prefix)
    registry.register("ffhq/64", partial(_load_ffhq, resolution=64), prefix=prefix)
    registry.register("ffhq/32", partial(_load_ffhq, resolution=32), prefix=prefix)