"""Run only in the exact candidate wheel interpreter, with no source package path."""
from importlib import metadata
from pathlib import Path
import pytest
import prosaic_runtime as api


@pytest.mark.skipif('site-packages' not in Path(api.__file__).parts,reason='installed-origin checkpoint only')
def test_parallel_candidate_is_installed():
    assert 'site-packages' in Path(api.__file__).parts
    assert metadata.version('b3-prosaic-runtime')==api.__version__
    assert 'parallel_tool_calls_v1' in api.ProsaicRuntime.capabilities


from test_parallel_tool_wires import test_actual_parallel_batch_preserves_correlated_result_order
from test_parallel_tool_turns import test_parallel_frozen_identity_roundtrip_and_drift,test_parallel_evidence_roundtrips_with_closed_observation
