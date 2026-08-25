"""A chip-style tag entry with completion.

Imports go through ``aqt.qt`` rather than PyQt6 directly so the widget keeps
working on Qt5 builds of Anki, and the chips wrap onto further lines instead
of pushing the input field off the dialog once a handful of tags are set.
"""

from __future__ import annotations

from aqt.qt import *


class FlowLayout(QLayout):
    """Left-to-right layout that wraps, à la Qt's own flowlayout example."""

    def __init__(self, parent=None, margin=0, spacing=4) -> None:
        super().__init__(parent)
        self._items: list[QLayoutItem] = []
        self.setContentsMargins(margin, margin, margin, margin)
        self.setSpacing(spacing)

    def addItem(self, item: QLayoutItem) -> None:  # noqa: N802 (Qt API)
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802 (Qt API)
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802 (Qt API)
        if 0 <= index < len(self._items):
            return self._items.pop(index)
        return None

    def expandingDirections(self) -> Qt.Orientation:  # noqa: N802 (Qt API)
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802 (Qt API)
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802 (Qt API)
        return self._layout(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802 (Qt API)
        super().setGeometry(rect)
        self._layout(rect, apply=True)

    def sizeHint(self) -> QSize:  # noqa: N802 (Qt API)
        return self.minimumSize()

    def minimumSize(self) -> QSize:  # noqa: N802 (Qt API)
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        margins = self.contentsMargins()
        return size + QSize(
            margins.left() + margins.right(), margins.top() + margins.bottom()
        )

    def _layout(self, rect: QRect, apply: bool) -> int:
        margins = self.contentsMargins()
        effective = rect.adjusted(
            margins.left(), margins.top(), -margins.right(), -margins.bottom()
        )
        x = effective.x()
        y = effective.y()
        line_height = 0
        spacing = self.spacing()

        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > effective.right() and line_height > 0:
                x = effective.x()
                y = y + line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_height = max(line_height, hint.height())

        return y + line_height - rect.y() + margins.bottom()


class TagChip(QFrame):
    def __init__(self, tag: str, owner: "TagInputWidget") -> None:
        super().__init__()
        self.tag = tag
        self.owner = owner

        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 2, 4, 2)
        layout.setSpacing(4)

        label = QLabel(tag)
        label.setToolTip("Double-click to edit")
        label.mouseDoubleClickEvent = self._edit  # type: ignore[assignment]

        remove = QToolButton()
        remove.setText("×")
        remove.setAutoRaise(True)
        remove.clicked.connect(self.remove_self)

        layout.addWidget(label)
        layout.addWidget(remove)

    def remove_self(self) -> None:
        self.owner.remove_tag(self.tag)

    def _edit(self, _event) -> None:
        self.owner.input.setText(self.tag)
        self.owner.remove_tag(self.tag)
        self.owner.input.setFocus()


class TagInputWidget(QWidget):
    tagChanged = pyqtSignal()

    def __init__(self, tag_list: list[str]) -> None:
        super().__init__()
        self.tags: list[str] = []

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        self.chip_area = QWidget()
        self.chip_layout = FlowLayout(self.chip_area)
        outer.addWidget(self.chip_area)

        self.input = QLineEdit()
        self.input.setPlaceholderText("Type a tag and press Enter")
        self.input.returnPressed.connect(self.add_tag_from_input)

        completer = QCompleter(sorted(tag_list))
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.activated.connect(self.input.setText)
        self.input.setCompleter(completer)

        outer.addWidget(self.input)

    def add_tag_from_input(self) -> None:
        tag = self.input.text().strip()
        self.input.clear()
        if not tag or tag in self.tags:
            return
        self.tags.append(tag)
        self.chip_layout.addWidget(TagChip(tag, self))
        self.chip_area.updateGeometry()
        self.tagChanged.emit()

    def remove_tag(self, tag: str) -> None:
        if tag in self.tags:
            self.tags.remove(tag)
        for index in reversed(range(self.chip_layout.count())):
            item = self.chip_layout.itemAt(index)
            widget = item.widget() if item else None
            if isinstance(widget, TagChip) and widget.tag == tag:
                self.chip_layout.takeAt(index)
                widget.setParent(None)
                widget.deleteLater()
        self.chip_area.updateGeometry()
        self.tagChanged.emit()

    def get_tags(self) -> list[str]:
        return list(self.tags)

    def clear_tags(self) -> None:
        for tag in list(self.tags):
            self.remove_tag(tag)
