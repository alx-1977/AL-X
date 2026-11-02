"""Direct coverage of the guard shared by reference and KiCad readers."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

from alx.providers import credential_paths


SSH_KEY_NAMES = (
    'id_rsa', 'id_rsa_sk', 'id_dsa', 'id_dsa_sk',
    'id_ecdsa', 'id_ecdsa_sk', 'id_ed25519', 'id_ed25519_sk',
)


@pytest.mark.parametrize('name', SSH_KEY_NAMES)
@pytest.mark.parametrize('case', ['lower', 'upper', 'title'])
@pytest.mark.parametrize('template', ['{}', '/home/user/.ssh/{}', 'nested/{}/notes.txt'])
def test_ssh_key_names_are_denied(name, case, template):
    # Protected names apply to every path component, including directories.
    path = template.format(getattr(name, case)())
    assert credential_paths.denied(path), path


@pytest.mark.parametrize('name', [
    '.alx', '.env', '.env.local', '.netrc', '.npmrc', 'id_rsa.backup',
    'certificate.pem', 'certificate.key', 'certificate.p12', 'certificate.pfx',
    'secrets.json', 'credentials.json', 'tokens.json', 'passwords.txt',
])
@pytest.mark.parametrize('case', ['lower', 'upper', 'title'])
def test_other_credential_components_are_denied(name, case):
    component = getattr(name, case)()
    assert credential_paths.denied(f'nested/{component}')
    assert credential_paths.denied(f'nested/{component}/notes.txt')


@pytest.mark.parametrize('path', [
    'README.md', 'src/alx/providers/reference_file.py',
    '/home/user/projects/board.kicad_pcb', 'footprints/Connector.pretty/pad.kicad_mod',
    'references/report.pdf', 'nested/environment.txt',
])
def test_ordinary_reference_and_design_paths_are_allowed(path):
    assert not credential_paths.denied(path)
