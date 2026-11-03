import re
from pathlib import Path

from armadok.worker_protocol.error_codes import WORKER_ERROR_CODES


def test_every_code_a_worker_can_raise_is_on_the_allowed_list():
    # A code missing from the list would be turned into a generic error by the hub.
    used = set()
    for path in Path("armadok/worker").glob("*.py"):
        used |= set(re.findall(r'"(worker\.[a-z_]+)"', path.read_text()))
    assert used - WORKER_ERROR_CODES == set(), "add these to error_codes.WORKER_ERROR_CODES"
