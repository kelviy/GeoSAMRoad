"""The docked review queue panel: one key per decision, over the review
service. It holds no review logic: every key sends one request and the panel
shows the state that comes back."""

from qgis.core import QgsApplication
from qgis.PyQt.QtCore import QEvent, Qt
from qgis.PyQt.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDockWidget,
    QFormLayout,
    QGridLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .client import ReviewClient, ServiceError
from .zone import HandEditsNotSaved, Zone

# Button key -> (label, the item kinds it acts on, what it does).
# What it does is a decision for a flag, or "tile", "back" or "forward".
ACTIONS = {
    "A": ("Accept", ("proposal",), "real"),
    "X": ("Reject", ("proposal",), "not_defect"),
    "R": ("Real", ("flag",), "real"),
    "N": ("Not a defect", ("flag",), "not_defect"),
    "Q": ("Needs QGIS", ("proposal", "flag"), "needs_qgis"),
    "T": ("Reviewed", ("tile",), "tile"),
    "B": ("Back", ("proposal", "flag", "tile", None), "back"),
    "F": ("Forward", ("proposal", "flag", "tile"), "forward"),
}

# Turns the Sentinel-2 RGB on or off.
S2_KEY = "S"

FILTER_LABELS = {
    "kind": "Any kind",
    "flag_type": "Any flag type",
    "source": "Any source",
}


class ReviewPanel(QDockWidget):
    def __init__(self, canvas, client: ReviewClient, parent=None):
        super().__init__("ROSA review queue", parent)
        self.setObjectName("RosaReviewQueuePanel")
        self.canvas = canvas
        self.client = client
        self.zone: Zone | None = None
        self.state: dict | None = None

        body = QWidget()
        body.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        layout = QVBoxLayout(body)
        self.zone_label = QLabel()
        self.annotator_label = QLabel()
        self.item_label = QLabel()
        self.item_label.setWordWrap(True)
        self.item_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        self.position_label = QLabel()
        self.progress_label = QLabel()
        self.progress_label.setWordWrap(True)
        self.message = QLabel()
        self.message.setWordWrap(True)
        for label in (
            self.zone_label,
            self.annotator_label,
            self.item_label,
            self.position_label,
            self.progress_label,
        ):
            layout.addWidget(label)

        buttons = QGridLayout()
        self.buttons = {}
        for n, (key, (text, _, _)) in enumerate(ACTIONS.items()):
            button = QPushButton(f"{text} ({key})")
            button.clicked.connect(lambda _=False, k=key: self.act(k))
            buttons.addWidget(button, n // 2, n % 2)
            self.buttons[key] = button
        layout.addLayout(buttons)

        self.note = QLineEdit()
        self.note.setPlaceholderText(
            "Tile note (optional). Enter marks the tile reviewed"
        )
        self.note.returnPressed.connect(lambda: self.act("T"))
        layout.addWidget(self.note)

        self.show_s2 = QCheckBox(f"Sentinel-2 RGB over the aerial ({S2_KEY})")
        self.show_s2.toggled.connect(lambda _: self._show())
        layout.addWidget(self.show_s2)

        filters = QFormLayout()
        self.filters = {}
        for field, label in FILTER_LABELS.items():
            box = QComboBox()
            box.addItem(label, None)
            box.currentIndexChanged.connect(lambda _: self.refresh())
            filters.addRow(field.replace("_", " ").capitalize(), box)
            self.filters[field] = box
        layout.addLayout(filters)
        self.hidden_label = QLabel()
        self.hidden_label.setWordWrap(True)
        layout.addWidget(self.hidden_label)

        reload_button = QPushButton("Reload queue")
        reload_button.clicked.connect(self.refresh)
        layout.addWidget(reload_button)
        layout.addWidget(self.message)
        layout.addStretch()
        self.body = body
        self.setWidget(body)
        # The keys work while the panel has focus, except in the note field.
        for widget in (body, *body.findChildren(QWidget)):
            if widget is not self.note:
                widget.installEventFilter(self)
        self.bind(None)

    def eventFilter(self, watched, event):  # QObject calls it by this name
        """Take the panel's keys before QGIS's own shortcuts see them. QGIS
        binds S, T and R window-wide, and Qt fires neither of two shortcuts
        that match one key, so the panel claims the key instead: accepting the
        ShortcutOverride makes Qt deliver it as a key press."""
        if event.type() not in (QEvent.Type.ShortcutOverride, QEvent.Type.KeyPress):
            return super().eventFilter(watched, event)
        key = event.text().upper()
        if event.modifiers() & ~Qt.KeyboardModifier.KeypadModifier or (
            key not in ACTIONS and key != S2_KEY
        ):
            return super().eventFilter(watched, event)
        event.accept()
        if event.type() == QEvent.Type.KeyPress and not event.isAutoRepeat():
            if key == S2_KEY:
                self.show_s2.toggle()
            else:
                self.act(key)
        return True

    @property
    def annotator(self) -> str:
        return QgsApplication.userFullName().strip()

    def bind(self, zone: Zone | None):
        """Work on ``zone``, or show that no zone project is open."""
        self.zone = zone
        self.state = None
        self.zone_label.setText(
            f"<b>{zone.name}</b>" if zone else "Open a zone project (<zone>.qgs)."
        )
        self._say("")
        if zone is not None:
            zone.outline_selection()
            self.refresh()
        else:
            self._render()

    def refresh(self):
        self._send("state")

    def act(self, key: str):
        if self.zone is None or self.state is None:
            return
        _, kinds, what = ACTIONS[key]
        item = self.state["item"]
        kind = item["kind"] if item else None
        if kind not in kinds:
            self._say(f"{key} does not apply to {_kind_name(kind)}.", error=True)
            return
        if what in ("back", "forward"):
            self._send(what)
            return
        assert item is not None  # only back acts with no item
        if what == "tile":
            note = self.note.text().strip() or None
            if self._send("review_tile", writes=True, fid=item["fid"], note=note):
                self.note.clear()
        else:
            self._send("decide", writes=True, fid=item["fid"], decision=what)

    def _send(self, op: str, writes: bool = False, **args) -> bool:
        """Send one request and show the state it answers with. A write saves
        the annotator's open edits first and reloads the layers after."""
        if self.zone is None:
            return False
        if writes:
            if not self.annotator:
                self._say(
                    "Your computer account has no full name, and QGIS records "
                    "it as the annotator. Set the account's full name in your "
                    "operating system and restart QGIS.",
                    error=True,
                )
                return False
            args["annotator"] = self.annotator
        try:
            if writes:
                self.zone.save_hand_edits()
            answer = self.client.ask(op, self.zone.gpkg, filter=self._filter(), **args)
        except (HandEditsNotSaved, ServiceError) as exc:
            self._say(str(exc), error=True)
            return False
        finally:
            if writes:
                self.zone.reload()
        if not answer["ok"]:
            self._say(answer["error"], error=True)
            return False
        self.state = answer["state"]
        if answer.get("moved") is False:
            self._say("No item that way.")
        elif answer.get("deleted"):
            self._say(f"Deleted label rows {', '.join(map(str, answer['deleted']))}.")
        else:
            self._say("")
        self._render()
        self._show()
        return True

    def _filter(self) -> dict:
        return {
            field: box.currentData()
            for field, box in self.filters.items()
            if box.currentData() is not None
        }

    def _show(self):
        if not self.isVisible():
            return
        if self.zone is not None and self.state is not None:
            self.zone.show(self.canvas, self.state["item"], self.show_s2.isChecked())
        self.body.setFocus()

    def _render(self):
        state = self.state
        item = state["item"] if state else None
        self.annotator_label.setText(
            f"Annotator: {self.annotator or '(no account full name)'}"
        )
        kind = item["kind"] if item else None
        for key, (_, kinds, _) in ACTIONS.items():
            self.buttons[key].setEnabled(state is not None and kind in kinds)
        self.note.setEnabled(kind == "tile")
        if state is None:
            for label in (self.item_label, self.position_label, self.progress_label):
                label.setText("")
            self.hidden_label.setText("")
            return
        self.item_label.setText(_describe(item))
        self.position_label.setText(
            f"Item {state['index'] + 1} of {state['length']}"
            if item
            else f"Every one of the {state['length']} items is done."
        )
        p = state["progress"]
        self.progress_label.setText(
            f"{p['done']}/{p['total']} done, {p['remaining']} remaining: "
            f"flags {p['flags_decided']}/{p['flags_total']} decided, kept tiles "
            f"{p['tiles_reviewed']}/{p['tiles_kept']} reviewed. "
            f"{state['needs_qgis']} flag(s) marked needs_qgis."
        )
        hidden = ", ".join(state["hidden"])
        self.hidden_label.setText(f"Hidden detectors: {hidden}" if hidden else "")
        for field, box in self.filters.items():
            if box.count() == 1:
                box.blockSignals(True)
                for value in state["choices"][field]:
                    box.addItem(value, value)
                box.blockSignals(False)

    def _say(self, text: str, error: bool = False):
        self.message.setStyleSheet("color: #c0392b;" if error else "")
        self.message.setText(text)


def _kind_name(kind: str | None) -> str:
    return {"proposal": "a proposal", "flag": "a flag", "tile": "a tile"}.get(
        kind or "", "an empty queue"
    )


def _describe(item: dict | None) -> str:
    if item is None:
        return "Nothing left in this queue."
    if item["kind"] == "tile":
        reviewed = "reviewed" if item["reviewed"] else "not reviewed"
        return f"<b>Kept tile {item['tile_id']}</b> ({reviewed})"
    what = "Proposal" if item["kind"] == "proposal" else "Flag"
    return (
        f"<b>{what}: {item['type']}</b> from {item['detector']} ({item['source']}), "
        f"flag {item['fid']}, {item['decision']}"
    )
