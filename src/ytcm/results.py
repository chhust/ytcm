import os
import re
import shutil

import matplotlib.figure
import matplotlib.pyplot as plt

FIGURES_ROOT = "figures"
OUTPUT_ROOT = "output"


def slug(text, limit=60):
    text = re.sub(r"[^A-Za-z0-9]+", "-", (text or "").strip()).strip("-").lower()
    return text[:limit] or "figure"


class Collector:

    def __init__(self, name, adopt=None):
        self.directory = os.path.join(FIGURES_ROOT, name)
        self.adopt = adopt
        self.saved = []
        self.replaced = None
        self.warn_limit = None

        def capture(*arguments, **keywords):
            self.save_open_figures()
            self.show_without_blocking()

        self.capture = capture

    def title_of(self, figure):
        suptitle = getattr(figure, "_suptitle", None)
        title = suptitle.get_text() if suptitle is not None else ""
        if not title:
            title = next((axes.get_title() for axes in figure.axes
                          if axes.get_title()), "")
        return title

    def save_open_figures(self):
        for number in plt.get_fignums():
            figure = plt.figure(number)
            if getattr(figure, "ytcm_saved", False):
                continue
            path = os.path.join(
                self.directory, f"{len(self.saved):02d}__{slug(self.title_of(figure))}.png")
            figure.savefig(path, dpi=150, bbox_inches="tight")
            figure.ytcm_saved = True
            self.saved.append(path)

    def show_without_blocking(self):
        show = self.replaced[0]
        try:
            show(block=False)
        except TypeError:
            show()

    def adopt_written_figures(self):
        if not self.adopt or not os.path.isdir(self.adopt):
            return
        for name in sorted(os.listdir(self.adopt)):
            if name.lower().endswith(".png"):
                shutil.move(os.path.join(self.adopt, name),
                            os.path.join(self.directory, name))

    def __enter__(self):
        os.makedirs(self.directory, exist_ok=True)
        self.warn_limit = plt.rcParams["figure.max_open_warning"]
        plt.rcParams["figure.max_open_warning"] = 0
        self.replaced = (plt.show, matplotlib.figure.Figure.show)
        plt.show = self.capture
        matplotlib.figure.Figure.show = self.capture
        return self

    def __exit__(self, *details):
        try:
            self.save_open_figures()
            self.adopt_written_figures()
        finally:
            plt.show, matplotlib.figure.Figure.show = self.replaced
            plt.rcParams["figure.max_open_warning"] = self.warn_limit
        return False


def collect(name, adopt=None):
    return Collector(name, adopt)


def write_table(name, frame):
    if frame is None or not len(frame):
        return None
    os.makedirs(OUTPUT_ROOT, exist_ok=True)
    path = os.path.join(OUTPUT_ROOT, f"{name}.csv")
    frame.to_csv(path, index=False)
    return path
