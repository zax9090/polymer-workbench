"""Explicit operation allowlist for the disposable numerical worker."""
import hashlib
import json
from pathlib import Path
import sys


def structure_result(directory, analysis, parameters, job_id):
    from . import structure_analysis
    result = structure_analysis.ANALYSES[analysis](Path(directory), **parameters)
    directory = Path(directory)
    files = {name for name in (result["source"].get("data"), result["source"].get("trajectory")) if name}
    if analysis in {"msd", "widom"} and (directory / "bulk_complete.json").is_file():
        files.add("bulk_complete.json")
    def digest(path):
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    kernel = structure_analysis.native
    compiled = sys.modules.get(kernel.rdf_counts.__module__, kernel)
    from .paths import resource_root
    source = resource_root() / "workbench" / "structure_analysis.py"
    result["provenance"] = {"job_id": job_id, "parameters": parameters,
        "source_sha256": {name: digest(directory / name) for name in sorted(files)},
        "kernel_version": kernel.__version__, "kernel_sha256": digest(Path(compiled.__file__)),
        "analysis_code_sha256": digest(source) if source.is_file() else None}
    return result


def run(operation, payload):
    if operation == "structure":
        return structure_result(**payload)
    if operation in {"doe_design", "doe_analyze"}:
        from . import doe
        result = (doe.design if operation == "doe_design" else doe.analyze)(**payload)
        from .paths import resource_root
        kernel = sys.modules.get(doe.native.doe_fit.__module__, doe.native)
        def digest(path):
            if not path.is_file():
                return None
            with path.open("rb") as stream:
                return hashlib.file_digest(stream, "sha256").hexdigest()
        result["provenance"] = {"kernel_version": doe.native.__version__, "kernel_sha256": digest(Path(kernel.__file__)),
                                "analysis_code_sha256": digest(resource_root() / "workbench" / "doe.py")}
        return result
    if operation == "blend_contacts":
        from .blends import blend_chi
        payload = dict(payload)
        payload["scale"] = payload.pop("contact_scale", 1.0)
        return blend_chi(**payload)
    raise ValueError("지원하지 않는 계산 종류입니다.")


def main(folder):
    from .paths import install_worker_lifecycle
    install_worker_lifecycle()
    root = Path(folder)
    request = json.loads((root / "input.json").read_text())
    try:
        message = {"result": run(request["operation"], request["payload"])}
    except (ValueError, TypeError, OSError, ImportError) as exc:
        message = {"error": str(exc)}
    from .jobs import atomic_json
    atomic_json(root / "output.json", message)
    return 0
