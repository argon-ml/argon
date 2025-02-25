from argon.datasets import Dataset, DatasetRegistry, from_module
from argon.core.dataclasses import dataclass, field

from .tokenizer import Tokenizer

@dataclass
class NLPDataset(Dataset):
    tokenizer : Tokenizer = field(default=None)

datasets = DatasetRegistry[NLPDataset]()
datasets.extend("tinystories", from_module(".tinystories", "datasets"))