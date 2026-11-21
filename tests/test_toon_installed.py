"""Run only in the exact candidate wheel interpreter, with the toon extra and the released Harness."""
from importlib import metadata
from pathlib import Path
import pytest
import prosaic_runtime as api


@pytest.mark.skipif('site-packages' not in Path(api.__file__).parts, reason='installed-origin checkpoint only')
def test_toon_candidate_runs_under_the_released_harness():
    assert metadata.version('b3-prosaic-runtime') == api.__version__
    assert metadata.version('b3-prosaic-harness') == '0.9.12'
    assert metadata.version('toon-format')


from test_toon_harness import test_tool_step_pause_and_resume_send_toon_and_keep_json
from test_toon_evidence import (test_use_denied_after_staging_stops_before_the_result_is_sent,
    test_sent_result_has_a_receipt_for_the_request_bytes, test_required_citation_of_a_sent_result_is_accepted,
    test_compacted_result_is_not_marked_delivered)
