"""Named provider metadata and explicit OS-vault references, without network probes."""
import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

from .user_paths import user_directory


class ConnectionSetupError(ValueError):
    pass


def endpoint(value, provider):
    if provider == 'openai':
        if value:
            raise ConnectionSetupError('Native OpenAI uses its standard endpoint; choose openai-compatible for a custom URL.')
        return ''
    value = value or ('http://localhost:11434' if provider == 'ollama' else '')
    try:
        url = urlsplit(value)
        if (url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password
                or url.query or url.fragment or url.port == 0):
            raise ValueError()
    except ValueError:
        raise ConnectionSetupError('Endpoint must be HTTP(S), without credentials, query or fragment.') from None
    return value.rstrip('/')


@dataclass(frozen=True)
class ProviderProfile:
    name: str
    provider: str
    model: str
    base_url: str = ''
    credential_ref: str | None = None

    def validate(self):
        if not isinstance(self.name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,63}', self.name):
            raise ConnectionSetupError('Profile names must be 1–64 letters, digits, dots, underscores or hyphens.')
        if self.provider not in {'openai', 'ollama', 'openai-compatible'}:
            raise ConnectionSetupError('Choose openai, ollama or openai-compatible.')
        if not isinstance(self.model, str) or not self.model.strip() or len(self.model) > 256 or any(ord(c) < 32 for c in self.model):
            raise ConnectionSetupError('A model name of at most 256 characters is required.')
        if not isinstance(self.base_url, str) or len(self.base_url) > 2048:
            raise ConnectionSetupError('Invalid endpoint.')
        endpoint(self.base_url, self.provider)
        if self.provider != 'ollama' and (not isinstance(self.credential_ref, str)
                or not re.fullmatch(r'[a-f0-9]{32}', self.credential_ref)):
            raise ConnectionSetupError('Profile is missing its OS-keychain reference.')
        if self.provider == 'ollama' and self.credential_ref is not None:
            raise ConnectionSetupError('Ollama profiles do not use an API-key reference.')
        return self


@dataclass(frozen=True)
class ActiveConnection:
    provider: str
    model: str
    base_url: str = ''
    profile: str = 'ad-hoc'
    api_key: str | None = field(default=None, repr=False)
    saved: bool = False


class OSVault:
    service = 'ai-coding-agent'
    allowed = {
        ('keyring.backends.Windows', 'WinVaultKeyring'),
        ('keyring.backends.macOS', 'Keyring'),
        ('keyring.backends.SecretService', 'Keyring'),
        ('keyring.backends.kwallet', 'DBusKeyring'),
        ('keyring.backends.kwallet', 'DBusKeyringKWallet4'),
    }

    def backend(self):
        try:
            import keyring
            root = keyring.get_keyring()
            candidates = getattr(root, 'backends', [root])
            for candidate in candidates:
                cls = type(candidate)
                if (cls.__module__, cls.__name__) in self.allowed and candidate.priority > 0:
                    return candidate
        except Exception:
            raise ConnectionSetupError('OS keychain initialization failed. Install .[connections] and unlock/configure the OS vault, or use existing environment-variable flags.') from None
        raise ConnectionSetupError("OS keychain unavailable. Install .[connections], unlock/configure the OS vault, or use existing environment-variable flags. No plaintext fallback is used.")

    def get(self, reference):
        try:
            value = self.backend().get_password(self.service, reference)
            if not isinstance(value, str) or not value:
                raise ConnectionSetupError('API key is missing from the OS keychain; edit the profile to replace it.')
            return value
        except ConnectionSetupError:
            raise
        except Exception:
            raise ConnectionSetupError('Could not read the OS keychain. Unlock it and retry.') from None

    def set(self, reference, value):
        if not isinstance(value, str) or not value:
            raise ConnectionSetupError('An API key is required.')
        try:
            self.backend().set_password(self.service, reference, value)
        except ConnectionSetupError:
            raise
        except Exception:
            raise ConnectionSetupError('Could not save the API key in the OS keychain.') from None

    def delete(self, reference):
        try:
            self.backend().delete_password(self.service, reference)
        except Exception:
            raise ConnectionSetupError('Profile metadata was removed, but OS-keychain cleanup failed. Remove the old ai-coding-agent entry using the OS vault tools.') from None


class ProfileStore:
    def __init__(self, path=None, vault=None, warn=lambda message: None):
        self.path = Path(path) if path is not None else user_directory('config') / 'providers.json'
        self.vault = vault or OSVault()
        self.warn = warn

    def load(self):
        try:
            raw = self.path.read_text(encoding='utf-8')
        except FileNotFoundError:
            return {'version': 1, 'revision': 0, 'default': None, 'profiles': []}
        if len(raw) > 1000000:
            raise ConnectionSetupError('Provider configuration is oversized.')
        try:
            value = json.loads(raw)
            if (set(value) != {'version', 'revision', 'default', 'profiles'} or type(value['version']) is not int or value['version'] != 1
                    or type(value['revision']) is not int or value['revision'] < 0 or not isinstance(value['profiles'], list)):
                raise ValueError()
            profiles = [ProviderProfile(**item).validate() for item in value['profiles']]
            names = [p.name for p in profiles]
            if len(names) != len(set(names)) or value['default'] is not None and value['default'] not in names:
                raise ValueError()
            return value
        except (ValueError, TypeError, KeyError):
            raise ConnectionSetupError('Invalid provider configuration; repair providers.json before changing profiles.') from None

    def write(self, value, revision):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        lock = self.path.with_suffix('.lock')
        deadline = time.monotonic() + 2
        while True:
            try:
                descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(descriptor)
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise ConnectionSetupError('Provider configuration is locked. Retry; remove a stale providers.lock only after other agent processes have stopped.') from None
                time.sleep(0.05)
        temporary = None
        try:
            if self.load()['revision'] != revision:
                raise ConnectionSetupError('Provider profiles changed in another process. Retry the operation.')
            value = {**value, 'revision': revision + 1}
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=self.path.parent,
                                             prefix='.providers-', delete=False) as stream:
                temporary = stream.name
                json.dump(value, stream, ensure_ascii=True)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
            temporary = None
        finally:
            if temporary is not None:
                os.unlink(temporary)
            lock.unlink()

    def get(self, name=None):
        value = self.load()
        name = name or value['default']
        for item in value['profiles']:
            if item['name'] == name:
                return ProviderProfile(**item)
        raise ConnectionSetupError('Unknown provider profile. Use /providers or /connect.')

    def activate(self, profile):
        profile.validate()
        key = self.vault.get(profile.credential_ref) if profile.credential_ref else None
        return ActiveConnection(profile.provider, profile.model, endpoint(profile.base_url, profile.provider), profile.name, key, True)

    def save(self, name, provider, model, base_url='', key=None, default=True):
        current = self.load()
        old = next((p for p in current['profiles'] if p['name'] == name), None)
        reference = uuid4().hex if provider != 'ollama' else None
        profile = ProviderProfile(name, provider, model, endpoint(base_url, provider), reference).validate()
        if reference:
            if not key and old and old['provider'] == provider and old['credential_ref']:
                key = self.vault.get(old['credential_ref'])
            self.vault.set(reference, key)
        try:
            value = {**current, 'profiles': [p for p in current['profiles'] if p['name'] != name] + [asdict(profile)]}
            if default:
                value['default'] = name
            self.write(value, current['revision'])
        except BaseException:
            if reference:
                try:
                    self.vault.delete(reference)
                except ConnectionSetupError as exc:
                    self.warn(str(exc))
            raise
        if old and old['credential_ref']:
            try:
                self.vault.delete(old['credential_ref'])
            except ConnectionSetupError as exc:
                self.warn(str(exc))
        return ActiveConnection(provider, model, profile.base_url, name, key, True)

    def set_default(self, name):
        value = self.load()
        if not any(item['name'] == name for item in value['profiles']):
            raise ConnectionSetupError('Unknown provider profile. Use /providers or /connect.')
        self.write({**value, 'default': name}, value['revision'])

    def remove(self, name):
        value = self.load()
        item = next((item for item in value['profiles'] if item['name'] == name), None)
        if item is None:
            raise ConnectionSetupError('Unknown provider profile. Use /providers or /connect.')
        profile = ProviderProfile(**item)
        self.write({**value, 'profiles': [p for p in value['profiles'] if p['name'] != name],
                    'default': None if value['default'] == name else value['default']}, value['revision'])
        if profile.credential_ref:
            try:
                self.vault.delete(profile.credential_ref)
            except ConnectionSetupError as exc:
                self.warn(str(exc))


class ConnectionController:
    def __init__(self, creator, store=None, current=None, client=None):
        self.creator, self.store, self.current, self.client = creator, store or ProfileStore(), current, client

    def ready(self):
        if self.current is None:
            raise ConnectionSetupError('No provider is configured. Use /connect or explicit provider flags.')
        if self.client is None:
            self.client = self.creator(self.current)
        return self.client

    def select(self, name, ask, output):
        if name:
            candidate = self.store.activate(self.store.get(name))
        else:
            action = ask('Provider profiles: select / add / edit / remove', 'select').strip().lower()
            if action == 'remove':
                target = ask('Profile name')
                if self.current and target == self.current.profile:
                    raise ConnectionSetupError('Select another profile before removing the active one.')
                if ask('Remove this saved profile? Type remove', '') == 'remove':
                    self.store.remove(target)
                    output('Profile removed.')
                return None
            if action not in {'select', 'add', 'edit'}:
                raise ConnectionSetupError('Choose select, add, edit or remove.')
            target = ask('Profile name')
            if action == 'select':
                candidate = self.store.activate(self.store.get(target))
            else:
                old = self.store.get(target) if action == 'edit' else None
                if action == 'add' and any(p['name'] == target for p in self.store.load()['profiles']):
                    raise ConnectionSetupError('Profile already exists; choose edit.')
                provider = ask('Provider: openai / ollama / openai-compatible', old.provider if old else 'openai')
                model = ask('Model', old.model if old else ('gpt-4.1' if provider == 'openai' else ''))
                base = '' if provider == 'openai' else ask('Endpoint', old.base_url if old and old.provider == provider else ('http://localhost:11434' if provider == 'ollama' else ''))
                reference = uuid4().hex if provider != 'ollama' else None
                ProviderProfile(target, provider, model, endpoint(base, provider), reference).validate()
                key = ask('API key (blank keeps existing key when editing)', '', secret=True) if reference else None
                if reference and not key and old and old.provider == provider:
                    key = self.store.vault.get(old.credential_ref)
                if reference and not key:
                    raise ConnectionSetupError('An API key is required.')
                make_default = ask('Use as default profile? y/n', 'y').lower() == 'y'
                candidate = ActiveConnection(provider, model, endpoint(base, provider), target, key, True)
                # Check SDK/configuration construction before saving or activating; no inference.
                client = self.creator(candidate)
                candidate = self.store.save(target, provider, model, base, key, default=make_default)
                self.current, self.client = candidate, client
                output('Profile configured. No inference request was made; credentials have not been remotely verified.')
                return candidate
        client = self.creator(candidate)
        if ask('Use as default profile? y/n', 'y').lower() == 'y':
            self.store.set_default(candidate.profile)
        self.current, self.client = candidate, client
        output('Profile selected. No inference request was made.')
        return candidate
