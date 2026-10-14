"""Bounded JSONC parsing and byte-preserving object-member splices."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from installer.config import ShellError


@dataclass(frozen=True)
class Token:
    kind: str
    value: Any
    start: int
    end: int


@dataclass(frozen=True)
class ObjectMember:
    pointer: Tuple[str, ...]
    key_start: int
    value_start: int
    value_end: int
    comma: Token | None


@dataclass(frozen=True)
class JsoncDocument:
    data: Any
    value_ranges: Dict[Tuple[str, ...], Tuple[int, int]]
    object_closes: Dict[Tuple[str, ...], int]
    object_members: Dict[Tuple[str, ...], Tuple[ObjectMember, ...]]


def _tokens(text: str) -> List[Token]:
    tokens: List[Token] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or (index == 0 and char == "\ufeff"):
            index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "/":
            index += 2
            while index < len(text) and text[index] not in "\r\n":
                index += 1
            continue
        if char == "/" and index + 1 < len(text) and text[index + 1] == "*":
            end = text.find("*/", index + 2)
            if end < 0:
                raise ShellError("Agent JSONC configuration contains an unterminated comment")
            index = end + 2
            continue
        if char in "{}[]:,":
            tokens.append(Token(char, char, index, index + 1))
            index += 1
            continue
        if char == '"':
            start = index
            index += 1
            escaped = False
            while index < len(text):
                if escaped:
                    escaped = False
                elif text[index] == "\\":
                    escaped = True
                elif text[index] == '"':
                    index += 1
                    break
                index += 1
            else:
                raise ShellError("Agent JSONC configuration contains an unterminated string")
            try:
                value = json.loads(text[start:index])
            except ValueError as exc:
                raise ShellError("Agent JSONC configuration contains an invalid string") from exc
            tokens.append(Token("string", value, start, index))
            continue
        start = index
        while (
            index < len(text)
            and not text[index].isspace()
            and text[index] not in "{}[]:,"
            and text[index : index + 2] not in ("//", "/*")
        ):
            index += 1
        if index == start:
            raise ShellError("Agent JSONC configuration contains an invalid token")
        tokens.append(Token("atom", text[start:index], start, index))
    return tokens


def parse(text: str) -> JsoncDocument:
    tokens = _tokens(text)
    cursor = 0
    value_ranges: Dict[Tuple[str, ...], Tuple[int, int]] = {}
    object_closes: Dict[Tuple[str, ...], int] = {}
    object_members: Dict[Tuple[str, ...], Tuple[ObjectMember, ...]] = {}

    def take(kind: str) -> Token:
        nonlocal cursor
        if cursor >= len(tokens) or tokens[cursor].kind != kind:
            raise ShellError("Agent JSONC configuration has an invalid structure")
        token = tokens[cursor]
        cursor += 1
        return token

    def parse_value(pointer: Tuple[str, ...]) -> Tuple[int, int]:
        nonlocal cursor
        if cursor >= len(tokens):
            raise ShellError("Agent JSONC configuration ends unexpectedly")
        token = tokens[cursor]
        if token.kind == "{":
            opening = take("{")
            members: List[ObjectMember] = []
            seen = set()
            if cursor < len(tokens) and tokens[cursor].kind != "}":
                while True:
                    key = take("string")
                    name = str(key.value)
                    if name in seen:
                        raise ShellError("Agent JSONC configuration contains duplicate object keys")
                    seen.add(name)
                    take(":")
                    value_start, value_end = parse_value(pointer + (name,))
                    comma = take(",") if cursor < len(tokens) and tokens[cursor].kind == "," else None
                    members.append(
                        ObjectMember(pointer + (name,), key.start, value_start, value_end, comma)
                    )
                    if comma is None or (cursor < len(tokens) and tokens[cursor].kind == "}"):
                        break
            closing = take("}")
            value_ranges[pointer] = (opening.start, closing.end)
            object_closes[pointer] = closing.start
            object_members[pointer] = tuple(members)
            return opening.start, closing.end
        if token.kind == "[":
            opening = take("[")
            item = 0
            if cursor < len(tokens) and tokens[cursor].kind != "]":
                while True:
                    parse_value(pointer + (str(item),))
                    item += 1
                    comma = take(",") if cursor < len(tokens) and tokens[cursor].kind == "," else None
                    if comma is None or (cursor < len(tokens) and tokens[cursor].kind == "]"):
                        break
            closing = take("]")
            value_ranges[pointer] = (opening.start, closing.end)
            return opening.start, closing.end
        if token.kind not in {"string", "atom"}:
            raise ShellError("Agent JSONC configuration contains an invalid value")
        cursor += 1
        value_ranges[pointer] = (token.start, token.end)
        return token.start, token.end

    if not tokens:
        data: Any = {}
        return JsoncDocument(data, value_ranges, object_closes, object_members)
    parse_value(())
    if cursor != len(tokens):
        raise ShellError("Agent JSONC configuration contains trailing input")

    chars = list(text)
    index = 0
    in_string = False
    escaped = False
    while index < len(chars):
        char = chars[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue
        if char == '"':
            in_string = True
            index += 1
            continue
        if char == "/" and index + 1 < len(chars) and chars[index + 1] == "/":
            chars[index] = chars[index + 1] = " "
            index += 2
            while index < len(chars) and chars[index] not in "\r\n":
                chars[index] = " "
                index += 1
            continue
        if char == "/" and index + 1 < len(chars) and chars[index + 1] == "*":
            chars[index] = chars[index + 1] = " "
            index += 2
            while index + 1 < len(chars) and not (
                chars[index] == "*" and chars[index + 1] == "/"
            ):
                if chars[index] not in "\r\n":
                    chars[index] = " "
                index += 1
            chars[index] = chars[index + 1] = " "
            index += 2
            continue
        index += 1
    for token_index, token in enumerate(tokens[:-1]):
        if token.kind == "," and tokens[token_index + 1].kind in {"}", "]"}:
            chars[token.start : token.end] = " " * (token.end - token.start)
    if chars and chars[0] == "\ufeff":
        chars[0] = " "
    try:
        data = json.loads("".join(chars))
    except (TypeError, ValueError) as exc:
        raise ShellError("Agent JSONC configuration is invalid") from exc
    return JsoncDocument(data, value_ranges, object_closes, object_members)


def newline_for(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def line_indent(text: str, position: int) -> str:
    start = max(text.rfind("\n", 0, position), text.rfind("\r", 0, position)) + 1
    prefix = text[start:position]
    return prefix if prefix.strip() == "" else ""
