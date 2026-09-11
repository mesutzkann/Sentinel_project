"""QLoRA on Qwen2.5-1.5B-Instruct, so the router stops being the weakest thing in the agent.

    python -m training.train_router                    # train and merge, ~30 min on a 3060
    python -m training.train_router --epochs 5
    python -m training.train_router --no-merge         # adapter only

What it has to beat, measured on the same 225 test questions (`datasets/routing/_README.md`):
the keyword table at 0.449 intent accuracy, and this very model, prompted, at 0.316 — and
0.000 when the schema grammar is off, because the base model cannot produce the object unaided.

Three choices here are about accuracy rather than convenience.

**The loss is on the answer only.** The question is masked out, so no gradient is spent teaching
a 1.5B to reproduce text it will always be given. Training on the whole sequence is the usual way
to get a model that has learned the prompt's rhythm and not the mapping.

**The prompt is `router.v2`, which is one line.** The base model needs `router.v1` — fifteen
intents and their tool lists, 1.5 kB of schema — because it has to be told the vocabulary. A
tuned model has the vocabulary in its weights, and carrying the table at inference would be
paying 1029 ms for something it already knows. Each model is therefore benchmarked with the
prompt it ships with, which is the honest comparison and the one that reaches the 150 ms target.

**The label is `RouteDecision.model_dump_json()`.** Byte for byte what constrained decoding will
emit at inference, fields in schema order, no whitespace. A model trained on prettier JSON than
it is later forced to produce is a model fighting its own grammar.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from pathlib import Path

from routing.plans import decide
from routing.schema import Intent
from training.routing_dataset import DEFAULT_OUTPUT, Example, load

# ai-service/training/train_router.py -> ai-service -> repository root
_REPO_ROOT = Path(__file__).resolve().parents[2]

BASE_MODEL = "Qwen/Qwen2.5-1.5B-Instruct"
DEFAULT_OUT = _REPO_ROOT / "models" / "sentinel-router"

# From docs/planning.md §8. Targeting every projection rather than only q and v because the task
# is classification into a fixed vocabulary the base model does not have — the MLP is where that
# lands, and r=16 over seven modules is still under 40 MB of adapter.
LORA_R = 16
LORA_ALPHA = 32
LORA_DROPOUT = 0.05
LORA_TARGETS = ("q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj", "down_proj")

# Measured on the training set with Qwen's tokenizer and its chat template: the 99th percentile
# is 130 tokens and the longest is 134, with paraphrases capped at 160 characters. A window of
# 224 covers all of it; anything larger is padding nobody learns from and attention nobody needs.
MAX_LENGTH = 224


@dataclass(frozen=True, slots=True)
class Pair:
    """One training row as the trainer wants it: what is given, and what is to be produced."""

    prompt: str
    completion: str


def render_prompt(query: str) -> str:
    """The inference-time prompt, from the registry, so training cannot drift from serving."""
    from llm.prompts import registry

    return registry().get("router", "v2").render(query=query)


def to_pairs(examples: list[Example], tokenizer) -> list[Pair]:
    """Chat-templated prompts and the exact JSON the router is expected to answer with.

    The chat template matters: Qwen's instruct models are trained with it, and a fine-tune fed
    raw text teaches the model a second, contradictory convention — which shows up as a model
    that answers well when prompted the way it was trained and badly through Ollama, where the
    template is applied.
    """
    pairs: list[Pair] = []

    for example in examples:
        prompt = tokenizer.apply_chat_template(
            [{"role": "user", "content": render_prompt(example.query)}],
            tokenize=False,
            add_generation_prompt=True,
        )
        label = decide(Intent(example.intent), example.target_service).model_dump_json()
        pairs.append(Pair(prompt=prompt, completion=label + tokenizer.eos_token))

    return pairs


def build(args: argparse.Namespace):
    """Everything that needs a GPU, imported here so the module is importable without one."""
    import torch
    from peft import LoraConfig
    from transformers import AutoModelForCausalLM, AutoTokenizer, BitsAndBytesConfig

    tokenizer = AutoTokenizer.from_pretrained(args.base)
    tokenizer.padding_side = "right"

    quantisation = BitsAndBytesConfig(
        load_in_4bit=True,
        bnb_4bit_quant_type="nf4",
        # Quantising the quantisation constants too. Worth about 0.4 GB on a 1.5B, which on a
        # 6 GB card is the difference between a batch of two and a batch of one.
        bnb_4bit_use_double_quant=True,
        # bfloat16 rather than float16, and not a preference: fp16 training needs a gradient
        # scaler, PEFT hands it bf16 LoRA gradients, and torch has no fp16 unscale kernel for
        # them — the run dies on the first optimiser step. Ampere does bf16 natively and it needs
        # no scaler at all, which removes the whole class of problem rather than working around it.
        bnb_4bit_compute_dtype=torch.bfloat16,
    )

    model = AutoModelForCausalLM.from_pretrained(
        args.base,
        quantization_config=quantisation,
        dtype=torch.bfloat16,
        device_map={"": 0},
    )
    model.config.use_cache = False

    lora = LoraConfig(
        r=args.lora_r,
        lora_alpha=LORA_ALPHA,
        lora_dropout=LORA_DROPOUT,
        target_modules=list(LORA_TARGETS),
        bias="none",
        task_type="CAUSAL_LM",
    )

    return tokenizer, model, lora


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dataset", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--base", default=BASE_MODEL)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--epochs", type=float, default=3.0)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--batch", type=int, default=2, help="per device; 6 GB holds two")
    parser.add_argument("--accum", type=int, default=4, help="so the effective batch is eight")
    parser.add_argument("--lora-r", type=int, default=LORA_R)
    parser.add_argument(
        "--no-merge",
        action="store_true",
        help="stop at the adapter; merging is what Ollama can import",
    )
    parser.add_argument(
        "--merge-only",
        action="store_true",
        help="skip training and merge the adapter already in --output",
    )
    args = parser.parse_args(argv)

    if args.merge_only:
        merge(args, args.output / "adapter")

        return 0

    train = load(args.dataset, "train")
    val = load(args.dataset, "val")

    if not train:
        print(
            f"No training split in {args.dataset}. Build it with "
            f"`python -m training.routing_dataset`.",
            file=sys.stderr,
        )

        return 1

    from datasets import Dataset
    from trl import SFTConfig, SFTTrainer

    tokenizer, model, lora = build(args)

    def dataset(rows: list[Example]) -> Dataset:
        pairs = to_pairs(rows, tokenizer)

        return Dataset.from_list([{"prompt": p.prompt, "completion": p.completion} for p in pairs])

    train_set = dataset(train)
    val_set = dataset(val) if val else None

    print(f"{len(train_set)} training rows, {len(val_set or [])} validation", file=sys.stderr)
    print(f"example prompt:\n{train_set[0]['prompt']}", file=sys.stderr)
    print(f"example completion:\n{train_set[0]['completion']}", file=sys.stderr)

    config = SFTConfig(
        output_dir=str(args.output / "checkpoints"),
        num_train_epochs=args.epochs,
        per_device_train_batch_size=args.batch,
        gradient_accumulation_steps=args.accum,
        learning_rate=args.lr,
        lr_scheduler_type="cosine",
        # Steps rather than a ratio: this TRL's SFTConfig dropped `warmup_ratio`, and about thirty
        # steps is the same 3% of a three-epoch run over 1.4k rows at an effective batch of 8.
        warmup_steps=15,
        logging_steps=20,
        save_strategy="no",
        eval_strategy="epoch" if val_set else "no",
        per_device_eval_batch_size=args.batch,
        bf16=True,
        fp16=False,
        max_length=MAX_LENGTH,
        # The reason this is a prompt/completion dataset rather than a text one: TRL masks the
        # prompt, so the loss is only on the object the router has to produce.
        completion_only_loss=True,
        gradient_checkpointing=True,
        gradient_checkpointing_kwargs={"use_reentrant": False},
        report_to=[],
        seed=20260911,
    )

    trainer = SFTTrainer(
        model=model,
        args=config,
        train_dataset=train_set,
        eval_dataset=val_set,
        peft_config=lora,
    )

    trainer.train()

    adapter = args.output / "adapter"
    trainer.save_model(str(adapter))
    tokenizer.save_pretrained(str(adapter))
    print(f"Adapter saved to {adapter}", file=sys.stderr)

    if not args.no_merge:
        merge(args, adapter)

    return 0


def merge(args: argparse.Namespace, adapter: Path) -> None:
    """Fold the adapter into the base weights and write a model Ollama can import.

    Ollama reads a safetensors directory for supported architectures, so this is the whole of
    the export: no llama.cpp checkout, no GGUF conversion step to keep working. The Modelfile
    beside it pins temperature 0, because a router that answers differently on Tuesday is a
    router whose benchmark means nothing.
    """
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    print("Merging the adapter into the base weights...", file=sys.stderr)

    # bfloat16, matching what the adapter was trained in. Merging bf16 LoRA deltas into an fp16
    # base produced a model that emitted "@@@@@@@@" to every prompt and crashed llama.cpp's
    # grammar sampler — a 99.8% validation accuracy turned into 100% invalid JSON by one dtype.
    base = AutoModelForCausalLM.from_pretrained(args.base, dtype=torch.bfloat16, device_map="cpu")
    merged = PeftModel.from_pretrained(base, str(adapter)).merge_and_unload()

    # Qwen2.5-1.5B ties its output projection to its input embedding, so `save_pretrained`
    # writes no `lm_head.weight` at all. Ollama's safetensors importer does not reconstruct it
    # and produces a model that answers "@@@@@@@@" to everything — while the same weights, loaded
    # with transformers, route perfectly. Materialising the tied matrix costs about half a
    # gigabyte and is the difference between a model that works and one that looks broken.
    merged.config.tie_word_embeddings = False
    merged.lm_head.weight = torch.nn.Parameter(merged.model.embed_tokens.weight.clone())

    target = args.output / "merged"
    merged.save_pretrained(str(target), safe_serialization=True)
    AutoTokenizer.from_pretrained(args.base).save_pretrained(str(target))

    modelfile = args.output / "Modelfile"
    modelfile.write_text(
        "\n".join(
            [
                "FROM ./merged",
                "",
                "# Zero, and not a preference: the routing benchmark compares models, and a",
                "# sampler in the middle of that comparison measures the sampler.",
                "PARAMETER temperature 0",
                "PARAMETER top_p 1",
                "",
                '# Long enough for the largest route object (five tools) and no longer.',
                "PARAMETER num_predict 200",
                "",
            ]
        ),
        encoding="utf-8",
    )

    print(
        f"Merged model in {target}\n"
        f"Import it with:  ollama create sentinel-router -f {modelfile}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    raise SystemExit(main())
