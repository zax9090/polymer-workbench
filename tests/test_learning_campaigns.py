"""Isolated synthetic fixtures verify algorithms/state, never material accuracy."""
import copy
import json
import time
import uuid
from pathlib import Path

import numpy as np
import pytest

from workbench.campaigns import CampaignManager
from workbench.jobs import JobManager
from workbench.learning import SurrogateManager
from workbench.research_store import ResearchStore

SMILES = ['[*]CC[*]', '[*]CC(C)[*]', '[*]CO[*]', '[*]C(C)O[*]',
          '[*]C(F)C[*]', '[*]CC(Cl)[*]', '[*]C(C#N)C[*]', '[*]CC(c1ccccc1)[*]']


def fixtures(store, **changes):
    """Made-up targets in pytest's disposable database; not scientific training data."""
    rows = []
    for index, smiles in enumerate(SMILES):
        record = dict(material={'repeat_smiles': smiles, 'sample': f'TEST-{index}'}, property='tg',
                      value=300.0 + index*7, unit='K', method='TEST fixture DSC', conditions={'rate': 10},
                      origin='experimental', source='SYNTHETIC SOFTWARE TEST FIXTURE', verified=True)
        record.update(copy.deepcopy(changes))
        if 'protein' in record:
            record['material']['protein_id'] = record.pop('protein')
        rows.append(store.add_measurement(record))
    return rows


def test_train_group_separation_prediction_and_excluded_origins(tmp_path):
    store = ResearchStore(tmp_path / 'db')
    rows = fixtures(store)
    extra = {k: v for k,v in rows[0].items() if k not in {'id','created_at'}}
    extra['material'] = {'repeat_smiles': SMILES[0], 'sample': 'RENAMED DUPLICATE'}
    extra.pop('material_id'); store.add_measurement(extra)
    extra.update(origin='demo', value=9999); store.add_measurement(extra)
    extra.update(origin='experimental', verified=False); store.add_measurement(extra)
    manager = SurrogateManager(tmp_path, store)
    model = manager.train({'property': 'tg'})
    assert model['validation']['n_groups'] == 8
    assert model['validation']['n_measurements'] == 9
    assert len(model['excluded']) == 2
    assert model['is_multifidelity'] is False
    for split in model['validation']['splits']:
        assert not set(split['train_material_ids']) & set(split['test_material_ids'])
        assert not set(split['train_chemistries']) & set(split['test_chemistries'])
    result = manager.predict(model['id'], {'repeat_smiles': SMILES[0]})
    assert np.isfinite(result['value']) and result['unit'] == 'K'
    assert result['uncertainty_kind'] == 'uncalibrated_gp_posterior_std'
    assert result['applicability']['is_validated_domain'] is False
    assert len(manager.list()) == 1
    path = manager._path(model['id']); persisted = json.loads(path.read_text())
    persisted['training'][0]['y'] += 1; path.write_text(json.dumps(persisted))
    with pytest.raises(ValueError, match='해시'):
        manager.predict(model['id'], {'repeat_smiles': SMILES[0]})


def test_replicates_do_not_replace_chemical_diversity_and_conditions(tmp_path):
    store = ResearchStore(tmp_path / 'db')
    fixtures(store, material={'repeat_smiles': SMILES[0]})
    manager = SurrogateManager(tmp_path, store)
    with pytest.raises(ValueError, match='최소 8개'):
        manager.train({'property': 'tg'})
    fixtures(store, conditions={'rate': 20})
    with pytest.raises(ValueError, match='조건이 다릅니다'):
        manager.train({'property': 'tg'})
    model = manager.train({'property': 'tg', 'conditions': {'rate': 20}})
    assert model['validation']['n_groups'] == 8


def test_protein_context_required_and_exact_at_prediction(tmp_path):
    store = ResearchStore(tmp_path / 'db')
    fixtures(store, property='binding_affinity', unit='nM', protein='TEST-PROTEIN-A')
    manager = SurrogateManager(tmp_path, store)
    model = manager.train({'property': 'binding_affinity'})
    assert model['domain'] == 'protein_polymer'
    with pytest.raises(ValueError, match='protein_context'):
        manager.predict(model['id'], {'repeat_smiles': SMILES[0], 'protein_context': 'TEST-PROTEIN-B'})
    assert manager.predict(model['id'], {'repeat_smiles': SMILES[0], 'protein_context': 'TEST-PROTEIN-A'})['unit'] == 'nM'
    fixtures(store, property='binding_affinity', unit='nM', protein='TEST-PROTEIN-B')
    with pytest.raises(ValueError, match='다른 단백질'):
        manager.train({'property': 'binding_affinity'})


def test_md_training_requires_matching_actual_dynamics_and_units(tmp_path):
    store = ResearchStore(tmp_path / 'db')
    rows = fixtures(store)
    experiments = []
    for i, row in enumerate(rows):
        row['job_id'] = f'{i:032x}'
        experiments.append({'job_id': row['job_id'], 'status': 'completed', 'route': 'polymer_atomistic',
            'config': {'steps': 100, 'temperature_kelvin': 300}, 'result': {'engine': 'TEST', 'metadata': {'force_field': 'TEST-FF'}},
            'features': {'actual_dynamics': True, 'frame_count': 20, 'values': {'rg_mean': {'value': i+1., 'unit': 'angstrom', 'method': 'TEST explicit topology'}}}})
    # Stub only data sourcing to isolate compatibility checks; no fabricated MD files.
    store.list_measurements = lambda: copy.deepcopy(rows)
    store.list_experiments = lambda: copy.deepcopy(experiments)
    manager = SurrogateManager(tmp_path, store)
    model = manager.train({'property': 'tg', 'feature_keys': ['rg_mean']})
    with pytest.raises(ValueError, match='compatibility'):
        manager.predict(model['id'], {'repeat_smiles': SMILES[0], 'features': {'rg_mean': 1}})
    assert np.isfinite(manager.predict(model['id'], {'repeat_smiles': SMILES[0], 'features': {'rg_mean': 1}, 'compatibility': model['compatibility']})['value'])
    experiments[0]['features']['values']['rg_mean']['unit'] = 'nm'
    with pytest.raises(ValueError, match='분석법이 다릅니다'):
        manager.train({'property': 'tg', 'feature_keys': ['rg_mean']})
    experiments[0]['features']['values']['rg_mean']['unit'] = 'angstrom'
    experiments[0]['features']['actual_dynamics'] = False
    with pytest.raises(ValueError, match='최소 8개'):
        manager.train({'property': 'tg', 'feature_keys': ['rg_mean']})


def config(n):
    return {'name': f'fixture {n}', 'route': 'polymer_cg', 'monomers': n, 'chains': 1, 'steps': 20, 'engine': 'numpy'}


def payload(**changes):
    return {'name': 'isolated fixture campaign', 'candidates': [config(n) for n in (6,8,10)],
            'objective': {'source': 'feature', 'name': 'rg_mean', 'unit': 'sigma', 'direction': 'min'},
            'budget': 2, 'batch_size': 1, **changes}


class FakeJobs:
    def __init__(self): self.records = {}; self.cancelled = []
    def create(self, config):
        job = {'id': uuid.uuid4().hex, 'config': config, 'status': 'running'}
        self.records[job['id']] = job
        return copy.deepcopy(job)
    def get(self, key): return copy.deepcopy(self.records[key])
    def directory(self, key): return Path(key)
    def cancel(self, key): self.cancelled.append(key); self.records[key]['status'] = 'cancelled'


class FakeStore:
    def list_experiments(self): return []
    def list_measurements(self): return []
    def index_job(self, job, path):
        return {'job_id': job['id'], 'status': job['status'], 'route': 'polymer_cg',
                'features': {'values': {'rg_mean': {'value': float(job['config']['monomers']), 'unit': 'sigma', 'method': 'synthetic state-test fixture'}}}}


def test_campaign_bounds_units_dedup_batch_failure_restart(tmp_path):
    jobs = FakeJobs(); manager = CampaignManager(tmp_path, jobs, FakeStore())
    with pytest.raises(ValueError): manager.create(payload(budget=51))
    with pytest.raises(ValueError): manager.create(payload(objective=[]))
    with pytest.raises(ValueError, match='길이 단위'): manager.create(payload(objective={'source':'feature','name':'rg_mean','unit':'angstrom','direction':'min'}))
    record = manager.create(payload(candidates=[config(6), {**config(6), 'name': 'renamed'}, config(8)]))
    assert len(record['candidates']) == 2
    first = manager.iterate(record['id'])
    with pytest.raises(ValueError, match='배치'): manager.iterate(record['id'])
    job_id = first['iterations'][0]['job_ids'][0]; jobs.records[job_id]['status'] = 'failed'
    second = manager.iterate(record['id'])
    assert len(second['iterations']) == 2
    assert second['iterations'][1]['candidate_ids'] != second['iterations'][0]['candidate_ids']
    jobs.records[second['iterations'][1]['job_ids'][0]]['status'] = 'completed'
    done = manager.iterate(record['id'])
    assert done['status'] == 'completed' and done['attempted'] == 2 and len(jobs.records) == 2
    other = manager.create(payload())
    other['automatic'] = True; other['status'] = 'running'
    other['candidates'][0]['status'] = 'submitting'
    manager._save(other); manager.close()
    restarted = CampaignManager(tmp_path, jobs, FakeStore())
    recovered = restarted.get(other['id'])
    assert recovered['status'] == 'paused' and recovered['automatic'] is False
    assert recovered['candidates'][0]['status'] == 'interrupted'
    restarted.close()


def test_real_numpy_campaign_automatic_finite_queue(tmp_path):
    jobs = JobManager(tmp_path / 'jobs'); store = ResearchStore(tmp_path / 'data')
    manager = CampaignManager(tmp_path / 'research', jobs, store)
    try:
        record = manager.create(payload())
        assert not jobs.list()
        manager.start(record['id'])
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            report = manager.get(record['id'])
            if report['status'] in {'completed','paused'}: break
            time.sleep(.2)
        assert report['status'] == 'completed', report
        assert report['attempted'] == 2 and len(jobs.list()) == 2
        observed = [c for c in report['candidates'] if c['objective'] is not None]
        assert len(observed) == 2
        assert all(c['objective_provenance']['feature']['unit'] == 'sigma' for c in observed)
        assert all(e['is_demo'] for e in store.list_experiments())
    finally:
        manager.close(); jobs.close()


def test_actual_baybe_initial_and_observed_recommendations():
    pytest.importorskip('baybe')
    record = {'seed': 42, 'iterations': [], 'objective': {'direction': 'min'}}
    rows = [{'id': str(i), 'vector': [float(i), 1.0], 'objective': float((i-3)**2) if i<5 else None} for i in range(8)]
    chosen, report = CampaignManager._recommend_baybe(record, rows, [], 2)
    assert len(chosen) == 2 and len({r['id'] for r in chosen}) == 2
    assert report['strategy'] == 'BayBE farthest point sampling'
    chosen, report = CampaignManager._recommend_baybe(record, rows[5:], rows[:5], 1)
    assert chosen[0]['id'] in {'5','6','7'}
    assert report['strategy'] == 'BoTorch acquisition' and report['comparable_outcomes'] == 5


def test_baybe_never_recommends_an_already_measured_candidate():
    # Monotonic Rg outcomes from the frozen-bundle campaign check, where the best
    # observed point used to be re-recommended and paused the campaign.
    pytest.importorskip('baybe')
    record = {'seed': 42, 'iterations': [{}], 'objective': {'direction': 'min'}}
    outcomes = {4: 1.127, 6: 1.852, 8: 2.371, 9: 2.522, 11: 2.995}
    observed = [{'id': str(n), 'vector': [float(n), 1.0, 1.0], 'objective': v} for n, v in outcomes.items()]
    remaining = [{'id': str(n), 'vector': [float(n), 1.0, 1.0], 'objective': None} for n in (5, 7, 10)]
    chosen, report = CampaignManager._recommend_baybe(record, remaining, observed, 3)
    assert sorted(c['id'] for c in chosen) == ['10', '5', '7']
    assert report['strategy'] == 'BoTorch acquisition'


def test_martini_mixture_campaign_one_hot_blocks_and_fixed_protein(tmp_path):
    manager = CampaignManager(tmp_path, FakeJobs(), FakeStore())
    base = {'route': 'mixture', 'bridge_track': 'martini3', 'bridge_mode': 'simulate', 'pdb_text': 'ATOM fixture',
            'secondary_structure': 'CC', 'chains': 1, 'box_nm': 8, 'steps': 1000}
    candidates = [{**base, 'name': f'{residue}{n}', 'polyply_residue': residue, 'monomers': n}
                  for residue in ('PEO', 'PS') for n in (8, 16)]
    record = manager.create({'name': 'mixture fixture', 'candidates': candidates, 'budget': 2, 'batch_size': 1,
                             'objective': {'source': 'feature', 'name': 'contact_count', 'unit': 'count', 'direction': 'max'}})
    vectors = {c['config']['name']: c['vector'] for c in record['candidates']}
    # [monomers, chains, temperature_kelvin, box_nm, salt_molar] + one-hot(PEO, PS)
    assert vectors['PEO8'] == [8.0, 1.0, 300.0, 8.0, 0.15, 1.0, 0.0]
    assert vectors['PS16'][-2:] == [0.0, 1.0]
    with pytest.raises(ValueError, match='simulate'):
        manager.create({'candidates': [{**base, 'bridge_mode': 'protein_cg'}],
                        'objective': {'source': 'feature', 'name': 'contact_count', 'unit': 'count', 'direction': 'max'}})
    with pytest.raises(ValueError, match='단백질 입력을 고정'):
        manager.create({'candidates': [candidates[0], {**candidates[1], 'pdb_text': 'ATOM other protein'}],
                        'objective': {'source': 'feature', 'name': 'contact_count', 'unit': 'count', 'direction': 'max'}})
    manager.close()
