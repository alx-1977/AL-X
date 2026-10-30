"""Compose local Particle tools independently of a cloud token."""
from dataclasses import dataclass

from alx.config.settings import ParticleFirmwareSettings
from alx.providers.particle_local import ParticleLocal
from alx.safety import AuthorityPolicy
from alx.tools.particle_local import DEFINITIONS, build_particle_local_executors


@dataclass(frozen=True)
class ParticleLocalRuntime:
    definitions: tuple
    policies: dict
    executors: dict
    permissions: frozenset
    provider: ParticleLocal
    settings: ParticleFirmwareSettings


def build_particle_local_runtime(call_id_source, environment, storage_root):
    settings = ParticleFirmwareSettings.from_environment(environment, storage_root)
    provider = ParticleLocal(settings.cli, settings.toolchains, settings.root, settings.repositories)
    permission = 'particle.local'
    return ParticleLocalRuntime(DEFINITIONS, {
        d.capability_id: AuthorityPolicy(frozenset({permission})) for d in DEFINITIONS
    }, build_particle_local_executors(provider, call_id_source), frozenset({permission}), provider, settings)
