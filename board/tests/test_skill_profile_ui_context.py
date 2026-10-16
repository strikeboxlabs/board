"""Ensure profile display bindings use the record guarded by their enclosing panel."""
from html.parser import HTMLParser
from pathlib import Path


class ProfileBindings(HTMLParser):
    void_tags = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__()
        self.stack = []
        self.bindings = []

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        guards = [item[1].get("x-if", "") for item in self.stack if item[0] == "template"]
        for attribute in ("x-if", "x-text"):
            expression = attributes.get(attribute, "")
            if ".skill_profiles" in expression:
                self.bindings.append((expression, guards))
        if tag not in self.void_tags:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


def test_profile_display_uses_the_record_available_in_each_panel():
    index = Path(__file__).parents[1] / "src" / "board" / "server" / "static" / "index.html"
    parser = ProfileBindings()
    parser.feed(index.read_text(encoding="utf-8"))
    records = set()
    for expression, guards in parser.bindings:
        if "selectedFactProducingIntent()" in expression:
            records.add("fact")
            assert any("selectedFactProducingIntent()" in guard and ".skill_profiles" not in guard for guard in guards)
        if "selectedIntentRecord()" in expression:
            records.add("intent")
            assert any("selectedNode.type === 'intent'" in guard for guard in guards)
    assert records == {"fact", "intent"}
