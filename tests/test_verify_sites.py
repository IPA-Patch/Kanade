"""Tests for the label resolver in ``tools.verify_sites``.

Recipe labels are the only durable anchor a hook site has: RVAs move on
every target build, so a label that resolves to the wrong overload silently
patches the wrong function. Google.Protobuf alone ships 15 ``MergeFrom``
overloads, which is why labels may pin a parameter list.
"""

from __future__ import annotations

from tools.verify_sites import (
    find_method,
    param_type,
    split_label,
    split_params,
    types_by_name,
)

INDEX = {
    "types": [
        {
            "name": "MessageExtensions",
            "namespace": "Google.Protobuf",
            "kind": "class",
            "tdi": 1,
            "line": 10,
            "methods": [
                {
                    "sig": "public static void MergeFrom(IMessage message, byte[] data)",
                    "rva": "0x1000",
                    "line": 11,
                },
                {
                    "sig": (
                        "internal static void MergeFrom(IMessage message, "
                        "ReadOnlySequence<byte> data, bool discardUnknownFields, "
                        "ExtensionRegistry registry)"
                    ),
                    "rva": "0x2000",
                    "line": 12,
                },
            ],
        },
        {
            "name": "TitleScene.<OnActivateAsync>d__10",
            "namespace": "",
            "kind": "struct",
            "tdi": 2,
            "line": 20,
            "methods": [
                {"sig": "private void MoveNext()", "rva": "0x3000", "line": 21},
            ],
        },
        {
            "name": "Evaluator",
            "namespace": "Game",
            "kind": "class",
            "tdi": 3,
            "line": 30,
            "methods": [
                {
                    "sig": "public void .ctor(string path, Settings settings)",
                    "rva": "0x4000",
                    "line": 31,
                },
            ],
        },
    ]
}


def _resolve(label: str):
    by_name = types_by_name(INDEX)
    type_name, method_name, params = split_label(label)
    hit = find_method(by_name, type_name, method_name, param_types=params)
    return None if hit is None else hit[1]["rva"]


def test_param_list_selects_one_overload():
    assert _resolve("MessageExtensions.MergeFrom(IMessage, byte[])") == "0x1000"
    assert (
        _resolve(
            "MessageExtensions.MergeFrom(IMessage, ReadOnlySequence<byte>, "
            "bool, ExtensionRegistry)"
        )
        == "0x2000"
    )


def test_label_without_params_matches_any_overload():
    assert _resolve("MessageExtensions.MergeFrom") == "0x1000"


def test_wrong_arity_does_not_resolve():
    assert _resolve("MessageExtensions.MergeFrom(IMessage)") is None


def test_namespace_qualified_type_resolves():
    assert _resolve("Google.Protobuf.MessageExtensions.MergeFrom(IMessage, byte[])") == "0x1000"


def test_nested_type_plus_notation_resolves():
    assert _resolve("TitleScene+<OnActivateAsync>d__10.MoveNext") == "0x3000"


def test_ctor_shorthand_resolves():
    assert _resolve("Evaluator.ctor") == "0x4000"


def test_split_params_respects_generic_commas():
    assert split_params("List<ValueTuple<string, float>> ranked, int topN") == (
        "List<ValueTuple<string, float>> ranked",
        "int topN",
    )


def test_param_type_strips_names_modifiers_and_defaults():
    assert param_type("ref ParseContext input") == "ParseContext"
    assert param_type("int maxLength = 180") == "int"
    assert param_type("ExtensionRegistry") == "ExtensionRegistry"
    assert param_type("List<ValueTuple<string, float>> ranked") == "List<ValueTuple<string, float>>"
