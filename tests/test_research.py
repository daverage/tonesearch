from __future__ import annotations

import pytest

from tonesearch import research


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(research.time, "sleep", lambda seconds: None)


def _queries(request):
    asked = []
    with pytest.raises(RuntimeError):
        research.web_notes(request, search=lambda q, *a, **k: asked.append(q) or [])
    return list(dict.fromkeys(asked))[:2]  # each search is retried on other engines


def test_gear_nobody_listed_is_still_evidence():
    page = ("<title>Example Band guitar rig</title>"
            "<p>The guitarist recorded the track through a Zorblax QX-57 amplifier.</p>"
            "<p>For the solo he stepped on a Quillfeather Mk7 fuzz pedal.</p>")
    found = research._extract_evidence(page, "Example Band")
    assert "Zorblax QX-57" in found and "Quillfeather Mk7" in found
    assert research._sentence_score("Example Band recorded it through a Zorblax amplifier.", {"example", "band"}) > 0


def test_qualifiers_survive():
    page = ("<title>Hillsong United gear</title>"
            "<p>The Stratocaster listed here is a modern equivalent, not the original recording guitar.</p>"
            "<p>The exact amplifier used on this recording is unknown.</p>")
    found = research._extract_evidence(page, "Hillsong United")
    assert "modern equivalent" in found and "unknown" in found


def test_short_spec_lines_and_labelled_rig_lists():
    page = ("<title>Roland JC-40 stereo combo amplifier</title>"
            "<li>JC-40: 40W stereo 2x10 combo.</li><li>Amplifier: Diezel VH4</li><li>Home</li><li>Support</li>"
            "<p>Stereo inputs let you run modelers and stereo effects straight into the JC-40.</p>")
    found = research._extract_evidence(page, "stereo combo amplifier gigging")
    assert "JC-40: 40W stereo 2x10 combo." in found and "Amplifier: Diezel VH4" in found
    assert "Stereo inputs let you" in found  # a spec page talking to the reader is still a spec
    assert "Home" not in found and "Support" not in found


def test_forum_posters_own_rigs_are_not_artist_evidence():
    page = ("<title>Muse Knights of Cydonia tone - forum</title>"
            "<p>I use a Deluxe Reverb with a Tube Screamer.</p>"
            "<p>Bellamy was using a Diezel VH4 on this tour.</p>")
    found = research._extract_evidence(page, "Knights of Cydonia Muse", forum=True)
    assert found == "Bellamy was using a Diezel VH4 on this tour."
    off_topic = "<title>Best amp for gigging?</title><p>Bellamy was using a Diezel VH4 on this tour.</p>"
    assert research._extract_evidence(off_topic, "Knights of Cydonia Muse", forum=True) == ""


def test_maker_spec_pages_count_without_the_request_in_every_sentence():
    page = ("<title>JC-40 Jazz Chorus stereo combo</title>"
            "<p>The JC-40 delivers 40 watts through two 10-inch speakers in true stereo.</p>"
            "<p>An effects loop and a stereo line output make it a dependable platform on stage.</p>")
    found = research._extract_evidence(page, "stereo pedal platform combo gigging")
    assert "40 watts" in found and "effects loop" in found


def test_a_dated_sentence_and_a_hedge_come_with_the_evidence():
    page = ("<title>United We Stand guitar gear</title>"
            "<p>The album was recorded in 2006 at Hillsong Church. The guitarists played a Vox AC30 through the sessions."
            " The exact AC30 model is unclear.</p><p>Hillsong Church has campuses in many cities.</p>")
    found = research._extract_evidence(page, "Hillsong United We Stand")
    assert found == ("The album was recorded in 2006 at Hillsong Church. The guitarists played a Vox AC30 through the "
                     "sessions. The exact AC30 model is unclear.")


def test_evidence_stays_within_its_budget():
    sentences = "".join(f"<p>Example Band recorded take {i} through a Zorblax QX-{i} amplifier and a fuzz pedal.</p>"
                        for i in range(60))
    found = research._extract_evidence(f"<title>Example Band rig</title>{sentences}", "Example Band")
    assert 0 < len(found) <= research.EVIDENCE_CHARS and "\n" not in found


def test_searches_mix_rigs_and_specs_only_when_the_request_could_be_either():
    assert _queries("Tom Quayle") == ["Tom Quayle guitarist amp pedals gear used",
                                      "Tom Quayle guitarist interview amplifier gear"]
    assert _queries("AC30") == ["AC30 guitarist amp pedals gear used", "AC30 guitar specifications"]
    assert _queries("stereo pedal platform combo gigging") == [
        "stereo pedal platform combo gigging specifications", "stereo pedal platform combo gigging review"]
    assert _queries("Periphery bass") == ["Periphery bass bassist bass amp pedals gear used",
                                          "Periphery bass bassist bass interview amplifier gear"]


def test_search_words_keep_names_whole():
    assert research._search_words("Knights of Cydonia") == "Knights of Cydonia"
    assert research._search_words("I want the guitar tone from Knights of Cydonia by Muse") == "guitar Knights of Cydonia by Muse"
    assert research._topic("Knights of Cydonia") == "Knights Cydonia"  # library keys are unchanged


def test_one_page_per_site_and_no_bass_forums_for_guitar_requests():
    results = [{"href": "https://www.talkbass.com/threads/pedal-platform.1/", "title": "Bass thread", "body": ""},
               {"href": "https://site.example/a", "title": "A", "body": ""},
               {"href": "https://site.example/b", "title": "B", "body": ""},
               {"href": "https://other.example/c", "title": "C", "body": ""}]
    fetched = []

    def evidence(href, topic):
        fetched.append(href)
        return "Example Band recorded it through a Zorblax amplifier."
    research.web_notes("Example Band", search=lambda *a, **k: results, evidence=evidence)
    assert fetched == ["https://site.example/a", "https://other.example/c"]
    fetched.clear()
    research.web_notes("Example Band bass", search=lambda *a, **k: results, evidence=evidence)
    assert "https://www.talkbass.com/threads/pedal-platform.1/" in fetched


def test_snippets_are_marked_as_weaker_evidence():
    results = [{"href": "https://blocked.example/rig", "title": "Rig",
                "body": "Example Band recorded the album through a Zorblax amplifier and a fuzz pedal"}]
    notes = research.web_notes("Example Band", search=lambda *a, **k: results, evidence=lambda href, topic: "")
    assert notes == ("- Rig: Search snippet only: Example Band recorded the album through a Zorblax amplifier and a "
                     "fuzz pedal (https://blocked.example/rig)")


def test_skipped_sources_keep_makers_pages():
    assert research._skip_source("https://line6.com/customtone/tone/12345/")
    assert not research._skip_source("https://line6.com/powercab/")
    assert research._skip_source("https://www.youtube.com/watch?v=x")
    assert not research._skip_source("https://www.reddit.com/r/Guitar/comments/x/")


def test_lyrics_credits_titles_and_bios_are_not_gear_evidence():
    words = {"knights", "cydonia"}
    assert research._sentence_score("Album: Black Holes and Revelations", words, True) == 0
    assert research._sentence_score("Yes, the song was certified 4x Multi-Platinum by the RIAA in 2021.", {"oceans"}) == 0
    assert research._sentence_score("Guitar chords Gadd9Yours and AYou are Bmmine, oh Gadd9mine, Oceans guide.", {"oceans"}, True) == 0
    assert research._sentence_score("Eddie Van Halen played an EVH 5150III amp through the tour.", {"halen"}) > 0
    assert research._sentence_score("The great Aunknown where feet may Gadd9fail In oceans Adeep my faith will Gadd9stand.",
                                    {"hillsong", "united", "stand"}, True) == 0
    assert research._sentence_score("Check out Tom Quayle's YouTube channel here for more lessons and news.",
                                    {"tom", "quayle"}) == 0
    assert research._sentence_score("Tom Quayle took up the guitar at the age of 15 after hearing his dad play.",
                                    {"tom", "quayle"}) == 0
    assert research._sentence_score("For more on Periphery, including their live itinerary, head to this location.",
                                    {"periphery"}) == 0
    assert research._sentence_score("Prima di tutto, dimmi quello che vuoi sugli Orthrelm e la musica.", {"orthrelm"}) == 0
    assert research._sentence_score("Nolly ran the bass through a DI and a compressor.", {"nolly"}) > 0
    page = "<title>How the Vox AC30 became a British amp classic</title><p>Subscribe for more amp stories.</p>"
    assert research._extract_evidence(page, "AC30") == ""  # the title says what the page is about, not what was used


_ARTICLE = """<html><head><title>Muse Knights of Cydonia guitar rig</title></head><body>
<nav><a href="/">Home</a> <a href="/gear">Gear</a> <a href="/amps">Amps</a></nav>
<article><h1>Knights of Cydonia: the guitar rig</h1>
<p>Knights of Cydonia closes Black Holes and Revelations, which Muse recorded in 2006 with producer Rich Costey.</p>
<p>Matt Bellamy recorded the main riff with his Manson guitar, which has a ZVEX Fuzz Factory built into the body,
running into a Diezel VH4 amplifier. The exact cabinet used in the studio is not documented.</p>
<p>Subscribe to our newsletter for weekly gear news and exclusive deals from our partners.</p>
<p>Live, the Diezel was paired with a Marshall JCM800 2203 for extra midrange bite on the bigger stages.</p>
</article><footer>Copyright 2026 Gear Site. All rights reserved.</footer></body></html>"""


def test_trafilatura_is_installed_and_does_the_extracting():
    assert research._trafilatura() is not None, "pip install -r requirements.txt: trafilatura is a requirement"
    text, extractor = research._main_text(_ARTICLE)
    assert extractor == "trafilatura" and "Diezel VH4" in text and "Home" not in text
    found = research._extract_evidence(_ARTICLE, "Knights of Cydonia Muse")
    assert "ZVEX Fuzz Factory" in found and "Diezel VH4" in found and "not documented" in found
    assert "newsletter" not in found and "Copyright" not in found and "\n" not in found


def test_research_still_works_without_trafilatura(monkeypatch):
    monkeypatch.setattr(research, "_TRAFILATURA", False)  # as if it weren't installed
    text, extractor = research._main_text(_ARTICLE)
    assert extractor == "parser" and "Diezel VH4" in text
    assert "Diezel VH4" in research._extract_evidence(_ARTICLE, "Knights of Cydonia Muse")


def test_a_failing_or_empty_trafilatura_falls_back_to_the_parser(monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("lxml could not parse this page")
    monkeypatch.setattr(research, "_TRAFILATURA", broken)
    assert research._main_text(_ARTICLE)[1] == "parser"
    monkeypatch.setattr(research, "_TRAFILATURA", lambda *a, **k: None)  # trafilatura skips very short pages
    assert research._main_text("<p>Periphery records bass with a Darkglass B7K preamp.</p>")[1] == "parser"


def test_forum_replies_are_read_once():
    thread = """<html><head><title>Knights of Cydonia tone - Forum</title></head><body><article>
<h1>Knights of Cydonia tone</h1><p>Does anyone know what Bellamy used on the Black Holes and Revelations tour?</p></article>
<div class="comments"><div class="comment"><p>I use a Deluxe Reverb with a Tube Screamer for this song at home.</p></div>
<div class="comment"><p>Bellamy was using a Diezel VH4 on this tour, with the Manson guitar into it every night.</p></div>
</div></body></html>"""
    found = research._extract_evidence(thread, "Knights of Cydonia", forum=True)
    assert found.count("Bellamy was using a Diezel VH4") == 1 and "Deluxe Reverb" not in found


def test_notes_are_one_line_per_source_whatever_the_page_returns():
    results = [{"href": f"https://s{i}.example/rig", "title": f"Source {i}", "body": ""} for i in range(2)]
    pages = {"https://s0.example/rig": "Example Band recorded through a Zorblax QX-57 amplifier.\nThe exact model is unclear.",
             "https://s1.example/rig": "Example Band toured with\na Frobnic FX-9 fuzz pedal."}  # distinct: copies are skipped
    notes = research.web_notes("Example Band", search=lambda *a, **k: results, evidence=lambda href, topic: pages[href])
    lines = notes.splitlines()
    assert len(lines) == 2 and all(line.startswith("- Source") and line.endswith("/rig)") for line in lines)


def test_pages_that_never_mention_instruments_or_gear_are_not_evidence():
    camcorder = ("<title>Panasonic AG-AC30 review</title><p>The AG-AC30 is a 1080p camcorder with a built-in LED light, "
                 "used by videographers and journalists for interviews and events.</p>")
    assert research._extract_evidence(camcorder, "AC30") == ""
    lyrics = ("<title>Hillsong United - Oceans lyrics</title><p>The song was recorded in 2013 and certified "
              "multi-platinum, and the band used it to close every concert on the tour that year.</p>")
    assert research._extract_evidence(lyrics, "Hillsong United") == ""
    results = [{"href": "https://cams.example/ac30", "title": "Panasonic AG-AC30",
                "body": "The AG-AC30 camcorder records 1080p video through a built-in XLR input used for interviews"}]
    with pytest.raises(RuntimeError):  # not even its search snippet counts
        research.web_notes("AC30", search=lambda *a, **k: results, evidence=lambda href, topic: "")


def test_a_sites_own_blurb_in_a_snippet_is_not_evidence():
    blurb = [{"href": "https://equipboard.example/pros/gavin-rossdale", "title": "Gavin Rossdale - Equipboard",
              "body": "1 day ago · This is a community-built gear list for Gavin Rossdale. Find relevant music gear like "
                      "Microphones, Guitars, Amplifiers, Effects Pedals, and other instruments and add it to Gavin Rossdale."}]
    with pytest.raises(RuntimeError):
        research.web_notes("Gavin Rossdale Bush guitar", search=lambda *a, **k: blurb, evidence=lambda href, topic: "")
    blurb[0]["body"] = "Gavin Rossdale played a Marshall JCM900 through most of the album sessions."
    assert "JCM900" in research.web_notes("Gavin Rossdale Bush guitar", search=lambda *a, **k: blurb,
                                          evidence=lambda href, topic: "")
