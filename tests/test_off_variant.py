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
    return evidence.measured_off(variant, record)["stems"]["snare.wav"]["sends"]


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


def pre_into_plate(other_send):
    """The snare sends to a pre bus routed into the plate; another source sends to the plate too."""
    found = recipe()
    found["stems"]["snare.wav"]["sends"] = [{"bus": "pre"}]
    found["buses"]["pre"] = {"bus": "plate"}
    found["buses"]["kit"].pop("sends")
    found["stems"]["tom.wav"] = {"bus": "kit"}
    other_send(found)
    return found


def test_a_shared_effect_after_the_return_keeps_the_sends_into_it():
    shared = pre_into_plate(lambda r: r["buses"]["kit"].update(sends=[{"bus": "plate"}]))
    shared["steps"].append({"id": "pre-hpf", "target": "pre", "plugin": "HighpassFilter"})
    assert snare_sends(shared, {"steps": ["pre-hpf"]})


def test_a_shared_effect_return_reached_through_another_bus_loses_every_send():
    shared = pre_into_plate(lambda r: r["stems"]["tom.wav"].update(sends=[{"bus": "plate"}]))
    off = evidence.measured_off(shared, {"steps": ["plate-verb"]})
    assert off["stems"]["snare.wav"]["sends"] == [] and off["stems"]["tom.wav"]["sends"] == []


def test_a_kept_effect_sending_into_an_emptied_return_is_muted_there():
    slap = recipe()
    slap["stems"]["snare.wav"]["sends"] = [{"bus": "slap"}]
    slap["buses"]["kit"].pop("sends")
    slap["buses"]["slap"] = {"sends": [{"bus": "plate"}]}
    slap["steps"].append({"id": "slap-delay", "target": "slap", "plugin": "Delay"})
    off = evidence.measured_off(slap, {"steps": ["plate-verb"]})
    assert off["stems"]["snare.wav"]["sends"] and off["buses"]["slap"]["sends"] == []


def test_only_the_emptied_branch_of_a_return_is_muted():
    split = pre_into_plate(lambda r: None)
    split["buses"]["pre"]["sends"] = [{"bus": "plate-a"}]
    split["buses"]["plate-a"] = {}
    split["steps"].append({"id": "verb-a", "target": "plate-a", "plugin": "Reverb"})
    off = evidence.measured_off(split, {"steps": ["verb-a"]})
    assert off["stems"]["snare.wav"]["sends"] and off["buses"]["pre"]["sends"] == []


def test_a_send_the_record_removes_cant_hide_another_dry_send():
    masked = recipe()
    masked["stems"]["snare.wav"]["sends"] = []
    masked["buses"]["snare"]["sends"] = [{"bus": "plate", "gain_db": 13}]
    masked["buses"]["kit"]["sends"] = [{"bus": "pre"}]
    masked["buses"]["pre"] = {"bus": "plate"}
    off = evidence.measured_off(masked, {"steps": ["plate-verb"], "fields": ["buses.kit.sends.pre"]})
    assert off["buses"]["snare"]["sends"] == []


def test_a_record_claiming_only_level_on_a_return_keeps_its_sends():
    trimmed = recipe()
    trimmed["steps"].append({"id": "plate-trim", "target": "plate", "plugin": "Gain", "settings": {"gain_db": -3}})
    assert snare_sends(trimmed, {"steps": ["plate-trim"]})


def test_what_a_drop_writes_never_edits_another_records_send():
    assert evidence.off_variant(recipe(), {"steps": ["plate-verb"]})["stems"]["snare.wav"]["sends"]
