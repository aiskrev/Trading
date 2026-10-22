import hashlib
from pathlib import Path
from typing import NamedTuple, Union


class PromptSpec(NamedTuple):
    prompt_id: str
    version: str
    text: str
    sha256: str

    @property
    def qualified_version(self) -> str:
        return f"{self.prompt_id}.{self.version}"


def load_prompt(prompts_dir: Union[str, Path], prompt_id: str, version: str) -> PromptSpec:
    text = (Path(prompts_dir) / f"{prompt_id}.{version}.md").read_text(encoding="utf-8")
    return PromptSpec(prompt_id, version, text, hashlib.sha256(text.encode("utf-8")).hexdigest())
