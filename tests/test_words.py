"""Phrases with listed words ("YouTube", "subscribe"…): which words match, how much is cut, how the cuts join."""
from vidaudcont.engine.analyzer import Settings, find_phrases, merge_cuts, word_list
from vidaudcont.timecodes import format_cuts


def say(text, start=0.5, step=0.4, pauses=None):
    """[[start, end, word, probability]] as Whisper gives them: one word every `step` s, longer pauses after the
    words numbered in `pauses` {index: seconds}."""
    out, t = [], start
    for i, w in enumerate(text.split()):
        out.append([round(t, 2), round(t + step - 0.1, 2), " " + w, 0.9])
        t += step + (pauses or {}).get(i, 0.0)
    return out


def cut_words(found, words):
    return [" ".join(w[2].strip() for w in words if f["start"] <= w[0] and w[1] <= f["end"]) for f in found]


def test_word_list_reads_what_the_user_typed():
    assert word_list("YouTube, Subscribe; like and  subscribe,\nsubscri*, , YouTube") == \
        ["youtube", "subscribe", "like and subscribe", "subscri*"]
    assert word_list("") == [] and word_list(" , ;") == []


def test_word_forms_count_but_other_words_do_not():
    entries = word_list("YouTube, subscribe, video, channel, actor, exam")
    heard = {"videos": True, "YouTube's": True, "subscribers": True, "subscribed": True, "actors": True,
             "exams": True, "channel,": True, "exam.": True,
             "examine": False, "examination": False, "example": False, "subscription": False, "act": False}
    for word, expected in heard.items():
        words = say(f"First sentence here. Then the {word} goes. And the end.")
        assert bool(find_phrases(words, entries, 30.0)) == expected, word
    assert find_phrases(say("Please like our subscription. Fine."), word_list("subscri*"), 30.0)
    two = say("Welcome to our You Tube page. Next.")                     # heard as two words
    assert cut_words(find_phrases(two, ["youtube"], 30.0), two) == ["Welcome to our You Tube page."]
    both = say("Please like and subscribe now. Thanks. Subscribe too.", pauses={4: 1.0, 5: 1.0})
    assert cut_words(find_phrases(both, ["like and subscribe"], 30.0), both) == ["Please like and subscribe now."]


def test_the_whole_sentence_goes_and_the_neighbours_stay():
    words = say("Hello doctor, my knee hurts. This video shows a consultation for knee pain. When did it start?",
                pauses={4: 1.3, 12: 1.3})
    found = find_phrases(words, ["video"], 30.0)
    assert cut_words(found, words) == ["This video shows a consultation for knee pain."]
    f = found[0]
    assert f["start"] == int(f["start"]) and f["end"] == int(f["end"])      # whole seconds, as in the table
    assert f["start"] >= words[4][1] and f["end"] <= words[13][0]          # inside the pauses around it
    assert f["reason"] == "фраза со словом «video»" and f["word"] == "video"
    only = find_phrases(words, ["video"], 30.0, whole=False)
    assert only[0]["reason"] == "слово «video»" and only[0]["end"] - only[0]["start"] <= 2


def test_no_full_stop_heard_a_pause_ends_the_phrase_and_a_long_one_is_cut_at_commas():
    words = say("thanks for watching this channel see you soon", pauses={4: 1.5})
    assert cut_words(find_phrases(words, ["channel"], 30.0), words) == ["thanks for watching this channel"]
    clauses = say("so " * 30 + "and in this video, we look at the chest " + "and so on " * 20, pauses={29: 1.0, 33: 1.0})
    assert cut_words(find_phrases(clauses, ["video"], 60.0), clauses) == ["and in this video,"]
    endless = say("so " * 30 + "and in this video we look at the chest " + "and so on " * 20)
    found = find_phrases(endless, ["video"], 60.0)
    assert "video" in cut_words(found, endless)[0] and found[0]["end"] - found[0]["start"] <= 12


def test_phrases_at_the_edges_go_to_start_and_end():
    words = say("Welcome back to the channel. Hello, how can I help? Remember to subscribe.", start=2.0,
                pauses={4: 1.0, 9: 1.0})
    found = find_phrases(words, ["channel", "subscribe"], 40.0)
    assert found[0]["start"] == 0.0 and found[-1]["end"] == 40.0
    assert format_cuts([(f["start"], f["end"]) for f in found], 40.0).startswith("start-")


def test_a_second_between_words_is_split_where_it_hurts_least():
    words = [[9.0, 10.6, " fine.", 0.9], [10.9, 11.4, " Subscribe", 0.9], [11.5, 12.2, " today.", 0.9],
             [12.3, 13.0, " Now", 0.9], [13.1, 14.0, " breathe.", 0.9]]
    f = find_phrases(words, ["subscribe"], 30.0)[0]
    assert (f["start"], f["end"]) == (11.0, 12.0)  # 0.1 s of "Subscribe" left rather than 0.6 s of "fine" lost


def test_cuts_join_like_the_sound_rules():
    st = Settings()
    base = [{"start": 0.0, "end": 5.0, "reasons": ["тишина"]}, {"start": 60.0, "end": 70.0, "reasons": ["музыка"]}]
    extra = [{"start": 9.0, "end": 12.0, "reason": "фраза со словом «video»"},
             {"start": 30.0, "end": 33.0, "reason": "фраза со словом «exam»"}]
    cuts = merge_cuts(base, extra, 100.0, st)
    assert [(c["start"], c["end"]) for c in cuts] == [(0.0, 12.0), (30.0, 33.0), (60.0, 70.0)]
    assert "короткий кусок между вырезами" in cuts[0]["reasons"] and "тишина" in cuts[0]["reasons"]
    assert merge_cuts(base, [], 100.0, st) == [dict(c, reasons=sorted(c["reasons"])) for c in base]
