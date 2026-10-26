#!/usr/bin/env python3
"""Assemble chunk_0003 from the 5 individual case files into one JSONL file.

Each case file is a standalone JSON object; one file -> one line in the chunk.
"""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
SRC_DIR = os.path.join(HERE, "cases_0003")
OUT = os.path.join(os.path.dirname(HERE), "chunks", "chunk_0003.jsonl")

order = [
    "case_0001.json",
    "case_0002.json",
    "case_0003.json",
    "case_0004.json",
    "case_0005.json",
]

with open(OUT, "w", encoding="utf-8") as out:
    for name in order:
        with open(os.path.join(SRC_DIR, name), encoding="utf-8") as f:
            case = json.load(f)
        out.write(json.dumps(case, ensure_ascii=False) + "\n")

print(f"wrote {OUT}")
