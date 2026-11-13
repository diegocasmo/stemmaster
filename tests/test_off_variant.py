import evidence


def recipe():
    return {
        "stems": {"snare.wav": {"bus": "snare", "sends": [{"bus": "plate", "gain_db": 13}]},
                  "kick.wav": {"bus": "kit"}},
        "buses": {"snare": {"bus": "kit"}, "kit": {"sends": [{"bus": "plate", "gain_db": -6}]}, "plate": {}},
        "steps": [{"id": "plate-verb", "target": "plate", "plugin": "Reverb"},
                  {"id": "kit-comp", "target": "kit", "plugin": "Compressor"}],
    }


def snare_sends(variant, record):
    return evidence.off_variant(variant, record)["stems"]["snare.wav"]["sends"]


def test_a_return_left_with_no_step_loses_every_send_into_it():
    assert snare_sends(recipe(), {"steps": ["plate-verb"], "fields": ["buses.kit.sends.plate"]}) == []


def test_a_return_left_with_only_level_on_it_loses_its_sends():
    leveled = recipe()
    leveled["steps"].append({"id": "plate-trim", "target": "plate", "plugin": "Gain", "settings": {"gain_db": -3}})
    assert snare_sends(leveled, {"steps": ["plate-verb"]}) == []


def test_a_return_still_processed_routed_into_or_feeding_its_own_effect_keeps_its_sends():
    still = recipe()
    still["steps"].append({"id": "plate-eq", "target": "plate", "plugin": "EQ"})
    assert snare_sends(still, {"steps": ["plate-verb"]})
    routed = recipe()
    routed["stems"]["kick.wav"]["bus"] = "plate"
    assert snare_sends(routed, {"steps": ["plate-verb"]})
    upstream = recipe()
    upstream["stems"]["snare.wav"]["sends"] = [{"bus": "pre"}]
    upstream["buses"]["pre"] = {"bus": "plate"}
    upstream["buses"]["kit"].pop("sends")
    upstream["steps"].append({"id": "pre-hpf", "target": "pre", "plugin": "HighpassFilter"})
    assert snare_sends(upstream, {"steps": ["pre-hpf"]})
    sent_on = recipe()
    sent_on["stems"]["snare.wav"]["sends"] = [{"bus": "pre"}]
    sent_on["buses"]["pre"] = {"sends": [{"bus": "plate"}]}
    sent_on["buses"]["kit"].pop("sends")
    sent_on["steps"].append({"id": "pre-hpf", "target": "pre", "plugin": "HighpassFilter"})
    assert snare_sends(sent_on, {"steps": ["pre-hpf"]})
