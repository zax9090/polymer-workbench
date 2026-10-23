"""Validation at the boundary; scientific adapters validate route-specific inputs."""
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, model_validator


class JobConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    name: str = Field(default="새 연구 작업", min_length=1, max_length=120)
    route: Literal["polymer_cg", "polymer_atomistic", "protein", "polyply", "hoomd", "pysimm", "packmol", "mixture"] = "polymer_cg"
    monomers: int = Field(default=24, ge=2, le=200)
    chains: int = Field(default=2, ge=1, le=16)
    steps: int = Field(default=1000, ge=10, le=100000)
    temperature: float = Field(default=1.0, ge=0.05, le=5)
    temperature_kelvin: float = Field(default=300, ge=1, le=2000)
    timestep: float = Field(default=0.002, ge=0.0001, le=0.005)
    seed: int = Field(default=42, ge=1, le=899999998)
    engine: Literal["auto", "lammps", "numpy"] = "auto"
    repeat_smiles: str = Field(default="[*]CC[*]", max_length=1000)
    sequence: str = Field(default="", max_length=10000)
    atomistic_mode: Literal["build", "simulate", "bulk", "crosslink"] = "build"
    atomistic_forcefield: Literal["gaff2", "pcff"] = "gaff2"
    charge_method: Literal["gasteiger", "xtb_cm5"] = "gasteiger"
    cell_builder: Literal["radonpy", "native"] = "radonpy"
    build_density: float = Field(default=0.8, ge=0.3, le=1.2)
    resin_smiles: str = Field(default="CC(C)(c1ccc(OCC2CO2)cc1)c1ccc(OCC2CO2)cc1", max_length=1000)
    hardener_smiles: str = Field(default="NCCN", max_length=1000)
    resin_count: int = Field(default=16, ge=2, le=400)
    hardener_count: int = Field(default=8, ge=1, le=400)
    cure_temperature_kelvin: float = Field(default=450, ge=300, le=600)
    target_conversion: float = Field(default=0.85, ge=0.05, le=0.99)
    reaction_cutoff_angstrom: float = Field(default=4.0, ge=3, le=6)
    cure_steps: int = Field(default=200000, ge=1000, le=5000000)
    protocol_scale: float = Field(default=1.0, ge=0.001, le=1.0)
    packmol_target: Literal["none", "sap_patch", "negative_patch", "positive_patch"] = "none"
    sap_radius_angstrom: float = Field(default=5.0, ge=3, le=15)
    protein_ph: float = Field(default=7.0, ge=0, le=14)
    ionic_strength_molar: float = Field(default=0.15, ge=0, le=5)
    protein_protonation: Literal["as_input", "propka"] = "as_input"
    protein_repair: Literal["none", "pdbfixer"] = "none"
    protein_mode: Literal["export", "predict", "import", "database"] = "export"
    accession: str = Field(default="P69905", pattern=r"^[A-Z0-9]{6,10}$")
    msa_mode: Literal["single_sequence", "server"] = "single_sequence"
    bfactor_is_plddt: bool = False
    ligand_smiles: str = Field(default="", max_length=1000)
    polyply_library: str = Field(default="martini3", pattern=r"^[A-Za-z0-9_]{1,50}$")
    polyply_residue: str = Field(default="PEO", pattern=r"^[A-Za-z0-9_]{1,30}$")
    forcefield_itp_text: str = Field(default="", max_length=2000000)
    mdp_text: str = Field(default="", max_length=100000)
    box_nm: float = Field(default=15, ge=5, le=100)
    source_job_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")
    source_artifact: str = Field(default="structure.pdb", pattern=r"^[A-Za-z0-9_.-]+$")
    pdb_text: str = Field(default="", max_length=5000000)
    a3m_text: str = Field(default="", max_length=5000000)
    engine_input: str = Field(default="", max_length=500000)
    bridge_track: Literal["martini3", "amber", "openff"] = "martini3"
    bridge_mode: Literal["prepare", "protein_cg", "simulate"] = "prepare"
    secondary_structure: str = Field(default="", max_length=10000, pattern=r"^[HEC]*$")
    polymer_net_charge: int = Field(default=0, ge=-100, le=100)
    equilibration_steps: int = Field(default=50000, ge=100, le=500000)
    salt_molar: float = Field(default=0.15, ge=0, le=1)
    contact_cutoff_nm: float = Field(default=0.6, ge=0.3, le=1.5)
    elastic_network: bool = False

    @model_validator(mode="after")
    def bounds(self):
        bulk = self.route == "polymer_atomistic" and self.atomistic_mode == "bulk"
        cure = self.route == "polymer_atomistic" and self.atomistic_mode == "crosslink"
        if self.route in {"polymer_atomistic", "pysimm"} and not bulk and not cure:
            if "chains" in self.model_fields_set and self.chains != 1:
                raise ValueError("전원자/RadonPy 및 pysimm 경로는 현재 한 사슬만 지원합니다. 사슬 수를 1로 설정하세요.")
            self.chains = 1
        if bulk:
            if self.chains < 2:
                raise ValueError("비정질 벌크 셀에는 사슬 2개 이상이 필요합니다.")
            if not 100 <= self.temperature_kelvin <= 500:
                raise ValueError("RadonPy 21단계 평형화의 최고 온도가 600 K이므로 최종 온도는 100~500 K로 설정하세요.")
        if self.cell_builder == "native" and not (bulk and self.atomistic_forcefield == "gaff2"):
            raise ValueError("자체 셀 빌더(native)는 GAFF2 비정질 벌크 셀에서만 사용합니다.")
        if self.charge_method == "xtb_cm5" and not (bulk and self.atomistic_forcefield == "gaff2"):
            raise ValueError("xtb CM5 전하는 GAFF2 비정질 벌크 셀에서만 사용합니다. PCFF는 결합 증분 전하를 씁니다.")
        if cure and not 100 <= self.temperature_kelvin <= 500:
            raise ValueError("경화 후 물성 샘플링 온도는 100~500 K로 설정하세요.")
        if self.route in ("polymer_cg", "hoomd") and not 0.05 <= self.temperature <= 5:
            raise ValueError("조대화 모델의 온도 T*는 0.05~5 범위입니다. 단위는 kBT/ε입니다.")
        if self.monomers * self.chains > 1000:
            raise ValueError("이 개인용 구성에서는 작업당 최대 1,000개 비드를 사용합니다.")
        if self.engine == "numpy" and (self.monomers * self.chains > 256 or self.steps > 20000):
            raise ValueError("NumPy 계산은 256개 비드 및 20,000 스텝 이하로 설정하세요. 큰 작업은 LAMMPS를 사용하세요.")
        if self.route == "protein" and self.protein_mode not in {"import", "database"}:
            seq = "".join(line.strip() for line in self.sequence.splitlines() if not line.startswith(">"))
            seq = "".join(seq.split()).upper()
            if not 4 <= len(seq) <= 2000 or any(c not in "ACDEFGHIKLMNPQRSTVWY" for c in seq):
                raise ValueError("단백질 서열에는 4~2,000개의 표준 아미노산 문자 또는 FASTA를 입력하세요.")
            self.sequence = seq
        if self.route == "mixture":
            if not 250 <= self.temperature_kelvin <= 400:
                raise ValueError("현재 혼합계 준비 템플릿의 온도 범위는 250~400 K입니다.")
            if self.bridge_mode in {"protein_cg", "simulate"} and self.bridge_track != "martini3":
                raise ValueError("단백질 CG 생성과 혼합계 MD는 Martini 3 경로에서 지원합니다.")
            if self.bridge_mode == "simulate" and 8.36 * self.box_nm ** 3 > 70000:
                raise ValueError("이 데스크톱 혼합계 MD는 약 7만 입자까지 지원합니다. 박스 한 변을 20 nm 이하로 줄이세요.")
            if self.bridge_track == "martini3" and self.polyply_library != "martini3":
                raise ValueError("Martini 3 브릿지에는 동일 버전의 martini3 라이브러리를 사용하세요.")
        return self
