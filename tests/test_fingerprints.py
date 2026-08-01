from app.services.fingerprints import FileObservation, source_fingerprint


def test_source_fingerprint_is_order_independent_and_metadata_only():
    first = [
        FileObservation("02.flac", 20, 200, "flac"),
        FileObservation("01.flac", 10, 100, "flac"),
    ]
    second = list(reversed(first))
    assert source_fingerprint(first) == source_fingerprint(second)
    assert source_fingerprint(first) != source_fingerprint(
        [FileObservation("01.flac", 11, 100, "flac"), first[0]]
    )
