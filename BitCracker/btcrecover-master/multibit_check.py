#!/usr/bin/env python3
"""Standalone MultiBit Classic wallet password checker.

A self-contained Python 3 CPU checker for MultiBit Classic `.key` backups. It
reimplements the MultiBit key derivation and a btcrecover-style tokenlist + typo
search in clean Python 3, with pycryptodome instead of the unmaintained PyCrypto,
so the recovery workflow no longer needs the vendored Python 2.7 btcrecover for
this wallet type.

The GPU tools (multibit_cuda_threads.exe, multibit_gpu.py) are much faster and are
the right choice for large searches. This tool exists for portability and for the
typo modes the GPU tools do not implement (replace, map).

Usage:
    python multibit_check.py --wallet multi.key --tokenlist search.txt
    python multibit_check.py --wallet multi.key --tokenlist search.txt \\
        --typos 2 --typos-capslock --typos-swap --typos-insert 0123456789
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import itertools
import os
import sys
from dataclasses import dataclass, field
from typing import Dict, Iterator, List, Optional, Tuple

try:
    from Crypto.Cipher import AES
except ImportError:
    sys.exit("This tool needs pycryptodome: pip install pycryptodome")


# ---------------------------------------------------------------------------
# MultiBit Classic crypto
# ---------------------------------------------------------------------------
#
# Key derivation is OpenSSL's EVP_BytesToKey with one MD5 iteration:
#     key1 = MD5(password + salt)
#     key2 = MD5(key1 + password + salt)
#     iv   = MD5(key2 + password + salt)
#     aes_key = key1 + key2                 (32 bytes, AES-256-CBC)
#
# A correct password decrypts the wallet's first block to a Bitcoin private key
# in WIF: the first byte is one of L, K, 5, Q and every byte is a valid base58
# character. Checking base58 validity is far cheaper than a full key parse and
# is what the search leans on.

_B58_CHARS = frozenset(b"123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")
_WIF_FIRST_BYTES = frozenset(b"LK5Q")


@dataclass
class MultiBitWallet:
    """The salt and ciphertext pulled from a MultiBit Classic .key file."""

    salt: bytes        # 8 bytes
    encrypted: bytes   # 32 bytes: the first two AES blocks

    @classmethod
    def load(cls, path: str) -> "MultiBitWallet":
        with open(path, "r") as f:
            raw = f.read(70)
        joined = "".join(raw.split())
        data = base64.b64decode(joined[:64])
        if not data.startswith(b"Salted__"):
            raise ValueError(f"{path} is not a MultiBit Classic .key file "
                             "(missing the 'Salted__' header)")
        if len(data) < 48:
            raise ValueError(f"{path} is too short to be a MultiBit .key file")
        return cls(salt=data[8:16], encrypted=data[16:48])

    def check(self, password: str) -> bool:
        """Return True if `password` decrypts the wallet to a valid WIF key.

        The password is encoded the way MultiBit does it: UTF-16-LE with the
        high byte of each unit dropped. For ASCII passwords that is just the
        ASCII bytes, which is the only case that decrypts cleanly (MultiBit
        Classic is known to mangle non-ASCII passwords).
        """
        pw = password.encode("utf-16-le", "ignore")[::2]
        salted = pw + self.salt
        key1 = hashlib.md5(salted).digest()
        key2 = hashlib.md5(key1 + salted).digest()
        iv = hashlib.md5(key2 + salted).digest()
        plaintext = AES.new(key1 + key2, AES.MODE_CBC, iv).decrypt(self.encrypted)
        if plaintext[0] not in _WIF_FIRST_BYTES:
            return False
        return all(b in _B58_CHARS for b in plaintext)


# ---------------------------------------------------------------------------
# Typo generation
# ---------------------------------------------------------------------------
#
# Same idea as btcrecover: start from a base password and produce every variant
# reachable with up to `max_typos` edits, drawn from the enabled typo kinds. The
# stages run in a fixed order and share the one budget, so the output covers
# every way of spending up to N typos across the enabled kinds.


@dataclass
class TypoSpec:
    max_typos: int = 0
    capslock: bool = False
    swap: bool = False
    repeat: bool = False
    delete: bool = False
    case: bool = False
    closecase: bool = False
    insert: str = ""                                  # charset to insert
    replace: str = ""                                 # charset to replace with
    typos_map: Dict[str, str] = field(default_factory=dict)

    @property
    def enabled(self) -> bool:
        return bool(self.max_typos and (
            self.capslock or self.swap or self.repeat or self.delete
            or self.case or self.closecase or self.insert or self.replace
            or self.typos_map))


def _swapcase(s: str) -> str:
    return s.swapcase()


def _case_id(ch: str) -> int:
    """0 = uncased, 1 = upper, 2 = lower."""
    if ch.isupper():
        return 1
    if ch.islower():
        return 2
    return 0


def _at_case_boundary(pw: str, i: int) -> bool:
    """True if pw[i] is cased and sits next to a case change or a string end."""
    cur = _case_id(pw[i])
    if cur == 0:
        return False
    if i == 0 or i + 1 == len(pw):
        return True
    prev, nxt = _case_id(pw[i - 1]), _case_id(pw[i + 1])
    return (prev not in (0, cur)) or (nxt not in (0, cur))


def _capslock_stage(pw: str, used: int, spec: TypoSpec) -> Iterator[Tuple[str, int]]:
    yield pw, used
    if spec.capslock and used < spec.max_typos:
        swapped = _swapcase(pw)
        if swapped != pw:
            yield swapped, used + 1


def _swap_stage(pw: str, used: int, spec: TypoSpec) -> Iterator[Tuple[str, int]]:
    yield pw, used
    if not spec.swap:
        return
    budget = min(spec.max_typos - used, len(pw) // 2)
    for k in range(1, budget + 1):
        for idxs in itertools.combinations(range(len(pw) - 1), k):
            # No two chosen indexes may be adjacent, or a single character would
            # be swapped twice in one variant.
            if any(b - a == 1 for a, b in zip(idxs, idxs[1:])):
                continue
            chars = list(pw)
            ok = True
            for i in idxs:
                if chars[i] == chars[i + 1]:   # swapping equal chars is a no-op
                    ok = False
                    break
                chars[i], chars[i + 1] = chars[i + 1], chars[i]
            if ok:
                yield "".join(chars), used + k


def _simple_options(pw: str, i: int, spec: TypoSpec) -> List[str]:
    """The replacement strings a single position can take under simple typos."""
    opts: List[str] = []
    if spec.repeat:
        opts.append(pw[i] * 2)
    if spec.delete:
        opts.append("")
    if spec.case:
        sc = _swapcase(pw[i])
        if sc != pw[i]:
            opts.append(sc)
    if spec.closecase and _at_case_boundary(pw, i):
        sc = _swapcase(pw[i])
        if sc != pw[i]:
            opts.append(sc)
    if spec.replace:
        opts.extend(c for c in spec.replace if c != pw[i])
    if spec.typos_map and pw[i] in spec.typos_map:
        opts.extend(spec.typos_map[pw[i]])
    return opts


def _simple_stage(pw: str, used: int, spec: TypoSpec) -> Iterator[Tuple[str, int]]:
    yield pw, used
    if not (spec.repeat or spec.delete or spec.case or spec.closecase
            or spec.replace or spec.typos_map):
        return
    budget = min(spec.max_typos - used, len(pw))
    for k in range(1, budget + 1):
        for positions in itertools.combinations(range(len(pw)), k):
            option_lists = [_simple_options(pw, i, spec) for i in positions]
            if any(not o for o in option_lists):
                continue
            for combo in itertools.product(*option_lists):
                out, prev = [], 0
                for pos, repl in zip(positions, combo):
                    out.append(pw[prev:pos])
                    out.append(repl)
                    prev = pos + 1
                out.append(pw[prev:])
                yield "".join(out), used + k


def _insert_stage(pw: str, used: int, spec: TypoSpec) -> Iterator[Tuple[str, int]]:
    yield pw, used
    if not spec.insert:
        return
    budget = min(spec.max_typos - used, len(pw) + 1)
    for k in range(1, budget + 1):
        # combinations_with_replacement lets more than one character be inserted
        # at the same gap.
        for gaps in itertools.combinations_with_replacement(range(len(pw) + 1), k):
            for chars in itertools.product(spec.insert, repeat=k):
                out, prev = [], 0
                for gap, ch in zip(gaps, chars):
                    out.append(pw[prev:gap])
                    out.append(ch)
                    prev = gap
                out.append(pw[prev:])
                yield "".join(out), used + k


def typo_variants(base: str, spec: TypoSpec) -> Iterator[str]:
    """Yield `base` and every typo variant of it, deduplicated."""
    if not spec.enabled:
        yield base
        return
    seen = set()
    stage1 = _capslock_stage(base, 0, spec)
    for pw1, u1 in stage1:
        for pw2, u2 in _swap_stage(pw1, u1, spec):
            for pw3, u3 in _simple_stage(pw2, u2, spec):
                for pw4, _ in _insert_stage(pw3, u3, spec):
                    if pw4 not in seen:
                        seen.add(pw4)
                        yield pw4


# ---------------------------------------------------------------------------
# Tokenlist parsing and base-password generation
# ---------------------------------------------------------------------------
#
# The tokenlist format matches btcrecover for the pieces this project uses:
#   + prefix        the line is required (one of its tokens must appear)
#   ^token          anchor to the first position
#   ^N^token        anchor to position N (1-indexed)
#   token$          anchor to the last position
#   %0,4d           digit wildcard, 0 to 4 digits
#   no + prefix     the line is optional and may be skipped
# Tokens on one line are alternatives; exactly one is chosen per combination.


@dataclass
class Token:
    text: str
    anchor: Optional[int]   # None = free, >=0 = fixed 0-indexed slot, -1 = last


@dataclass
class TokenLine:
    tokens: List[Token]
    required: bool


def _expand_digit_wildcard(spec: str) -> List[str]:
    """Expand %0,4d style specs into every concrete digit string."""
    body = spec[1:].rstrip("d")
    if "," in body:
        lo, hi = (int(x) for x in body.split(","))
    else:
        lo = hi = int(body or 0)
    out = [""] if lo == 0 else []
    for length in range(max(1, lo), hi + 1):
        out.extend("".join(d) for d in itertools.product("0123456789", repeat=length))
    return out


def _parse_token(tok: str) -> List[Token]:
    if tok.startswith("%") and tok.endswith("d"):
        return [Token(t, None) for t in _expand_digit_wildcard(tok)]
    if tok.endswith("$"):
        return [Token(tok[:-1], -1)]
    if tok.startswith("^"):
        body = tok[1:]
        head, sep, tail = body.partition("^")
        if sep and head.isdigit():
            return [Token(tail, int(head) - 1)]
        return [Token(body, 0)]
    return [Token(tok, None)]


def parse_tokenlist(path: str, delimiter: Optional[str] = None) -> List[TokenLine]:
    lines: List[TokenLine] = []
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for raw in f:
            line = raw.rstrip("\r\n")
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                continue
            required = stripped.startswith("+")
            if required:
                # Drop the "+" marker and the whitespace after it, so that
                # whitespace doesn't leak into the first token when a non-space
                # delimiter is in use.
                stripped = stripped[1:].lstrip()
            parts = stripped.split(delimiter) if delimiter else stripped.split()
            tokens: List[Token] = []
            for part in parts:
                tokens.extend(_parse_token(part))
            if tokens:
                lines.append(TokenLine(tokens, required))
    return lines


def _assemble(free: List[str], anchored: List[Tuple[int, str]]) -> Optional[str]:
    """Place anchored tokens at fixed slots and free tokens in the gaps."""
    total = len(free) + len(anchored)
    slots: List[Optional[str]] = [None] * total
    for pos, text in anchored:
        slot = total - 1 if pos == -1 else pos
        if not 0 <= slot < total or slots[slot] is not None:
            return None
        slots[slot] = text
    it = iter(free)
    for i in range(total):
        if slots[i] is None:
            slots[i] = next(it)
    return "".join(slots)


def generate_passwords(lines: List[TokenLine]) -> Iterator[str]:
    """Yield every base password the tokenlist describes (before typos)."""
    # Each line contributes one choice; optional lines add a "skip" choice.
    per_line_choices = [
        ([None] if not ln.required else []) + [t for t in ln.tokens]
        for ln in lines
    ]
    for chosen in itertools.product(*per_line_choices):
        free, anchored = [], []
        for tok in chosen:
            if tok is None:
                continue
            if tok.anchor is None:
                free.append(tok.text)
            else:
                anchored.append((tok.anchor, tok.text))
        if not free and not anchored:
            continue
        for order in itertools.permutations(free):
            pw = _assemble(list(order), anchored)
            if pw:
                yield pw


# ---------------------------------------------------------------------------
# Recovered-password output
# ---------------------------------------------------------------------------

def write_found_password(password: str) -> None:
    """Write the recovered password to a restricted file, not to stdout, so it
    doesn't linger in terminal scrollback or redirected logs."""
    path = "RECOVERED_PASSWORD.txt"
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(password)
    print(f"\nPassword found - written to {path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def build_typospec(args: argparse.Namespace) -> TypoSpec:
    return TypoSpec(
        max_typos=args.typos or 0,
        capslock=args.typos_capslock,
        swap=args.typos_swap,
        repeat=args.typos_repeat,
        delete=args.typos_delete,
        case=args.typos_case,
        closecase=args.typos_closecase,
        insert=args.typos_insert or "",
        replace=args.typos_replace or "",
    )


def search(wallet: MultiBitWallet, lines: List[TokenLine],
           spec: TypoSpec) -> Optional[str]:
    checked = 0
    for base in generate_passwords(lines):
        for candidate in typo_variants(base, spec):
            if wallet.check(candidate):
                return candidate
            checked += 1
            if checked % 100000 == 0:
                print(f"\r{checked:,} checked", end="", flush=True)
    print(f"\r{checked:,} checked")
    return None


def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(description="MultiBit Classic password checker (Python 3).")
    p.add_argument("--wallet", required=True, help="MultiBit Classic .key file")
    p.add_argument("--tokenlist", required=True, help="btcrecover-style token list")
    p.add_argument("--delimiter", help="token separator (default: whitespace)")
    p.add_argument("--typos", type=int, help="max typos per base password")
    p.add_argument("--typos-capslock", action="store_true")
    p.add_argument("--typos-swap", action="store_true")
    p.add_argument("--typos-repeat", action="store_true")
    p.add_argument("--typos-delete", action="store_true")
    p.add_argument("--typos-case", action="store_true")
    p.add_argument("--typos-closecase", action="store_true")
    p.add_argument("--typos-insert", metavar="CHARSET", help="characters to insert")
    p.add_argument("--typos-replace", metavar="CHARSET", help="characters to replace with")
    args = p.parse_args(argv)

    wallet = MultiBitWallet.load(args.wallet)
    lines = parse_tokenlist(args.tokenlist, args.delimiter)
    spec = build_typospec(args)

    found = search(wallet, lines, spec)
    if found is None:
        print("Password not found.")
        return 1
    write_found_password(found)
    return 0


if __name__ == "__main__":
    sys.exit(main())
