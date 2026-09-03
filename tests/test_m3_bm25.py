"""The keyword half of retrieval. This covers what vector search is unreliable
about."""

from halo.rag.bm25 import Bm25Index, tokenize

CORPUS = {
    "a": "Screen setup: $22.00 per colour, per location. PMS matching adds $15.00.",
    "b": "Embroidery is priced by stitch count. Digitizing is $65.00 one time.",
    "c": "Rush carries a surcharge of 25% to 40% of the decoration charge.",
    "d": "Sizes run S through 3XL. A 2XL upcharge applies above XL.",
}


def test_money_and_sizes_survive_tokenizing():
    """A tokenizer that split on punctuation would turn $22.00 into 22 and 00.
    That loses the fact that the question was about a price."""
    tokens = tokenize("What is the $22.00 setup and the 2XL upcharge?")
    assert "$22.00" in tokens
    assert "2xl" in tokens


def test_stopwords_are_dropped():
    assert tokenize("what is the charge for this") == ["charge"]


def test_an_exact_amount_finds_its_document():
    index = Bm25Index.build(CORPUS)
    assert index.search("$65.00 digitizing", limit=1)[0][0] == "b"


def test_a_rare_term_outranks_a_common_one():
    index = Bm25Index.build(CORPUS)
    top = index.search("surcharge", limit=1)
    assert top[0][0] == "c"


def test_a_query_with_no_matching_term_returns_nothing():
    """This is better than returning the closest document with a low score."""
    index = Bm25Index.build(CORPUS)
    assert index.search("kubernetes deployment topology") == []


def test_an_empty_index_is_not_an_error():
    assert Bm25Index.build({}).search("anything") == []
