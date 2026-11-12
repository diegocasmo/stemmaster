import numpy as np
import soundfile

import outcomes
import render
import structure

RATE = 48000


def noise(seconds, seed=0):
    return np.random.default_rng(seed).standard_normal(round(seconds * RATE)) * 0.1


def write(song, name, audio):
    soundfile.write(song / "stems" / name, np.asarray(audio), RATE, subtype="FLOAT")


def view(recipe, song, times=None):
    """A View as the check builds one: every stem and bus tapped, and a twin that renders another recipe the same way."""
    routing = structure.Routing(recipe)
    ctx = outcomes.Context(recipe, song, structure.nodes(recipe, routing, []), routing, times or {})

    def build(variant):
        result = render.Result(variant, song, sorted(render.sources(variant)), strict=variant is recipe)
        found = outcomes.View(ctx, result, None, variant)
        found.twin = build
        return found

    return build(recipe)
