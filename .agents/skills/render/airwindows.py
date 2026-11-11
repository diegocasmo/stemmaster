import re
import struct

WRAPPER = {"bypass", "input_level", "output_level", "mono_behaviour"}

# Airwindows Consolidated picks its effect only through saved state, not a parameter.
# The state is JUCE's "VC2!" blob wrapping XML in JUCE's own base64 variant.
ALPHABET = ".ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+"


def decode(text):
    size, data = text.split(".", 1)
    out = bytearray(int(size))
    for index, char in enumerate(data):
        value = ALPHABET.index(char)
        for bit in range(6):
            position = index * 6 + bit
            if value >> bit & 1 and position < len(out) * 8:
                out[position // 8] |= 1 << position % 8
    return bytes(out)


def encode(data):
    chars = []
    for start in range(0, len(data) * 8, 6):
        value = 0
        for bit in range(6):
            position = start + bit
            if position < len(data) * 8 and data[position // 8] >> position % 8 & 1:
                value |= 1 << bit
        chars.append(ALPHABET[value])
    return f"{len(data)}." + "".join(chars)


def select(plugin, effect):
    state = plugin.raw_state.decode("latin1")
    before, blob, after = re.match(r"(.*<IComponent>)(.*?)(</IComponent>.*)", state, re.S).groups()
    data = decode(blob)
    size = struct.unpack("<I", data[4:8])[0]
    xml, rest = data[8 : 8 + size].rstrip(b"\x00"), data[8 + size :]
    xml = re.sub(rb'currentProcessorName="[^"]*"', b'currentProcessorName="' + effect.encode() + b'"', xml) + b"\x00"
    plugin.raw_state = (before + encode(b"VC2!" + struct.pack("<I", len(xml)) + xml + rest) + after).encode("latin1")
    if f"currentProcessorName=\"{effect}\"" not in decode(
        re.search(r"<IComponent>(.*?)</IComponent>", plugin.raw_state.decode("latin1"), re.S).group(1)
    ).decode("latin1"):
        raise ValueError(f"Airwindows has no effect named {effect}")
