"""Static DOM/CSS contracts for live controls, not rendered browser assertions."""
from html.parser import HTMLParser
from pathlib import Path
import unittest

STATIC = Path(__file__).resolve().parents[1] / "src/wargame_native_engine/viewer/static"
VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


class Element:
    def __init__(self, tag, attrs=(), parent=None):
        self.tag, self.attrs, self.parent = tag, dict(attrs), parent
        self.children = []
        self.text = ""

    def has_class(self, value):
        return value in self.attrs.get("class", "").split()

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()


class Document(HTMLParser):
    def __init__(self, name):
        super().__init__()
        self.root = Element("document")
        self.stack = [self.root]
        self.feed((STATIC / name).read_text(encoding="utf-8"))

    def handle_starttag(self, tag, attrs):
        element = Element(tag, attrs, self.stack[-1])
        self.stack[-1].children.append(element)
        if tag not in VOID:
            self.stack.append(element)

    def handle_endtag(self, tag):
        if len(self.stack) > 1 and self.stack[-1].tag == tag:
            self.stack.pop()

    def handle_data(self, data):
        self.stack[-1].text += data

    def all(self):
        return list(self.root.descendants())

    def by_id(self, value):
        return next(node for node in self.all() if node.attrs.get("id") == value)


class CompactActionLayoutTests(unittest.TestCase):
    def test_operation_rail_is_sibling_of_map_not_below_canvas(self):
        for name in ("dual_control_index.html", "live_index.html"):
            with self.subTest(page=name):
                doc = Document(name)
                main = next(node for node in doc.all() if node.tag == "main")
                battle = next(node for node in doc.all() if node.has_class("battle-panel"))
                dock = next(node for node in doc.all() if node.has_class("operation-dock"))
                self.assertIs(battle.parent, main)
                self.assertIs(dock.parent, main)
                self.assertNotIn(dock, list(battle.descendants()))
                self.assertTrue(next(node for node in doc.all() if node.tag == "body").has_class("live-control-page"))
                self.assertFalse(any(node.tag == "style" for node in doc.all()), "inline old bottom-dock overrides must not return")

    def test_outer_confirmation_has_exact_submit_cancel_buttons(self):
        for name, ids in (("dual_control_index.html", ["execute-operation", "cancel-operation"]),
                          ("live_index.html", ["submit-action", "cancel-action"])):
            with self.subTest(page=name):
                doc = Document(name)
                group = doc.by_id("confirmation-controls")
                self.assertTrue(group.has_class("confirmation-box"))
                self.assertEqual(group.attrs.get("role"), "group")
                buttons = [node for node in group.children if node.tag == "button"]
                self.assertEqual([node.attrs["id"] for node in buttons], ids)
                self.assertEqual([node.text.strip() for node in buttons], ["√", "×"])
                self.assertEqual([node.attrs.get("aria-label") for node in buttons], ["下达指令", "取消指令"])
                self.assertTrue(all(node.attrs.get("type") == "button" for node in buttons))

    def test_css_right_rail_viewport_map_and_responsive_fallback(self):
        css = (STATIC / "live_styles.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"\.live-main,\s*\.product-live-main\s*\{[^}]*grid-template-columns:\s*minmax\(0, 1fr\) 272px")
        self.assertRegex(css, r"\.live-main > \.battle-panel\s*\{[^}]*height:\s*calc\(100dvh - 116px\)")
        self.assertRegex(css, r"\.live-main #battlefield\s*\{[^}]*height:\s*100%")
        self.assertRegex(css, r"\.operation-dock\s*\{[^}]*flex-direction:\s*column")
        self.assertRegex(css, r"\.operation-dock \.confirmation-box\s*\{[^}]*grid-template-columns:\s*1fr 1fr[^}]*border:")
        self.assertIn("@media (max-width: 900px)", css)
        self.assertIn("grid-template-columns: minmax(0, 1fr);", css)
        self.assertNotRegex(css, r"\.operation-dock\s*\{[^}]*grid-column:\s*1\s*/\s*-1")

    def test_weapon_panels_remain_conditional_and_confirmation_visibility_owned_by_controller(self):
        doc = Document("dual_control_index.html")
        for panel_id in ("direct-fire-panel", "guided-fire-panel", "indirect-fire-panel"):
            self.assertIn("hidden", doc.by_id(panel_id).attrs)
        css = (STATIC / "live_styles.css").read_text(encoding="utf-8")
        self.assertIn(".operation-dock[hidden], .operation-dock [hidden] { display: none; }", css)
        script = (STATIC / "dual_control.js").read_text(encoding="utf-8")
        self.assertIn('$("confirmation-controls").hidden', script)

    def test_legacy_observation_details_do_not_reserve_a_bottom_row(self):
        doc = Document("live_index.html")
        panel = next(node for node in doc.all() if node.has_class("observation-panel"))
        self.assertIn("hidden", panel.attrs)
        self.assertIn(".observation-panel[hidden] { display: none; }", (STATIC / "live_styles.css").read_text(encoding="utf-8"))
        for identifier in ("selected-unit-detail", "command-detail", "observation"):
            self.assertIn(doc.by_id(identifier), list(panel.descendants()))

    def test_replay_does_not_load_operational_layout(self):
        doc = Document("index.html")
        styles = [node.attrs.get("href", "") for node in doc.all() if node.tag == "link"]
        self.assertFalse(any("live_styles.css" in item for item in styles))
        self.assertFalse(any(node.has_class("operation-dock") for node in doc.all()))
        self.assertNotIn("confirmation-controls", {node.attrs.get("id") for node in doc.all()})


if __name__ == "__main__":
    unittest.main()
