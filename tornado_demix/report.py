"""CSV and self-contained HTML reports for every analysis.

CSV files carry a ``network`` column and UTC timestamps. HTML reports are
single files with inline CSS, so they can be attached to a case as they are.
"""

# The writers live in report_csv, report_html and report_json; this module keeps
# the one import path the CLI, the web UI and scripts use.

from .report_csv import (  # noqa: F401
    _sanitize_row,
    _write,
    candidate_cells,
    deposited_by_asset,
    evidence_families,
    format_fingerprint,
    format_span,
    format_totals,
    ranked_index,
    withdrawal_hashes,
    write_cluster_csv,
    write_demix_csv,
    write_multi_csv,
    write_relayer_csv,
)
from .report_html import (  # noqa: F401
    build_characterize_report,
    build_classify_report,
    build_cluster_report,
    build_html_report,
    build_multi_report,
    build_trace_report,
    terminal_bridge_text,
    write_characterize_report,
    write_classify_report,
    write_cluster_report,
    write_html_report,
    write_multi_report,
    write_trace_report,
)
from .report_json import (  # noqa: F401
    analysis_assumptions,
    demix_json,
    json_sha256,
    multi_json,
    write_demix_json,
    write_demix_json_hashed,
    write_multi_json,
)
