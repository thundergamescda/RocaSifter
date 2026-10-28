"""Package a selected Shared Pilot build; never install, launch, migrate or publish."""
import argparse
import base64
import hashlib
from importlib import metadata
import json
from pathlib import Path
import re
import sys
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from station_runtime import build_input_hashes

EVIDENCE_ALLOWLIST = frozenset({
    'READ_ME_FIRST.md', 'hosted-v3-acceptance.json', 'hosted-v3-provider-observed.log',
    'hosted-onboarding-acceptance.json', 'hosted-onboarding-acceptance.log',
    'hosted-auth-transitions-acceptance.json', 'hosted-auth-transitions-acceptance.log',
    'hosted-portal-races.json', 'dependency-audit.json', 'dependency-audit.log',
    'packaged-shared-readiness.json',
    'automated-tests.log', 'automated-summary.json',
    'shared-credit-user-acceptance.json', 'cloud-races.log',
    'intake-user-acceptance.log', 'intake-user-acceptance.json',
    'source-desktop-acceptance.log', 'source-desktop-acceptance.json',
    'packaged-desktop-acceptance.log', 'packaged-desktop-acceptance.json',
    'hosted-v2-acceptance.json', 'hosted-v2-provider-observed.log',
    'hosted-http-readiness.json',
    'import-user-acceptance.log', 'import-user-acceptance.json', 'import-focused-tests.log',
    'import-column-mapping.png', 'import-quantity-choice.png',
    'import-snapshot-confirmation.png', 'import-error-review.png',
    'import-user-acceptance.md', 'import-harness-history.md',
    'package-focused-tests.log', 'package-focused-tests.json',
    'receipt-lists-before.json', 'receipt-lists-after.json', 'receipt-lists-comparison.json',
    'receipt-cache-tests.log', 'receipt-cache-tests.json',
    'shared-trade-focused-tests.log', 'shared-trade-user-acceptance.log', 'shared-trade-user-acceptance.json',
    'shared-trade-offer-review.png', 'shared-trade-confirmed.png',
    'shared-trade-pending-recovery.png', 'shared-trade-cancelled.png', 'shared-trade-user-acceptance.md',
})
FORBIDDEN_PARTS = frozenset({
    '.git', '.venv', '__pycache__', 'station_data', 'outputs', 'backups',
    'profiles', 'profile', 'merchant', 'merchant_data', 'merchant-data', 'merchantdata',
    'cloud.session', 'cloud.pending', 'square.secret', 'vault.key',
    'roca_settings.json', 'installed_release.json', 'runtime.json', 'runtime.lock',
})
FORBIDDEN_SUFFIXES = {'.db', '.sqlite', '.sqlite3', '.csv', '.log', '.secret', '.key', '.token', '.pfx', '.p12', '.pdf', '.zpl'}
PROGRAM_JSON = {'_internal/desktop_build.json', '_internal/product_brand.json', '_internal/roca_edition.json'}
CRYPTOGRAPHY_VERSION = '50.0.2'
# These exact upstream metadata files are code-supply-chain evidence, not a
# general JSON exception. A dependency upgrade requires a fresh path review.
VENDOR_SBOMS = {
    '_internal/cryptography-50.0.2.dist-info/sboms/sbom.json':
        'cryptography-50.0.2.dist-info/sboms/sbom.json',
    '_internal/cryptography-50.0.2.dist-info/sboms/cryptography-rust.cyclonedx.json':
        'cryptography-50.0.2.dist-info/sboms/cryptography-rust.cyclonedx.json',
}
PROGRAM_JSON |= VENDOR_SBOMS.keys()
TOP_FILES = {'ProgramSHA256.json', 'EvidenceSHA256.json', 'package.json',
             'Install Shared Pilot.ps1', 'Install Shared Pilot.cmd', 'READ ME FIRST.txt'}


class PackageError(ValueError):
    pass


def digest_file(path):
    hashed = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            hashed.update(chunk)
    return hashed.hexdigest()


def safe_relative(value):
    if not isinstance(value, str) or not value or '\\' in value or ':' in value:
        raise PackageError('Unsafe payload path.')
    parts = value.split('/')
    for part in parts:
        if (not part or part in ('.', '..') or part[-1:] in (' ', '.')
                or any(ord(c) < 32 or c in '<>"|?*' for c in part)
                or re.fullmatch(r'(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part, re.I)):
            raise PackageError('Unsafe payload path.')
    return parts


def safe_program_path(value):
    parts = safe_relative(value)
    if any(p.casefold() in FORBIDDEN_PARTS or p.casefold() == '.env'
           or p.casefold().startswith(('.env.', 'testprofile-', 'merchant-', 'merchant_')) for p in parts):
        raise PackageError('Credential or merchant paths cannot be packaged.')
    if Path(parts[-1]).suffix.casefold() in FORBIDDEN_SUFFIXES:
        raise PackageError('Data or credential files cannot be packaged.')
    if Path(parts[-1]).suffix.casefold() == '.pem' and value != '_internal/certifi/cacert.pem':
        raise PackageError('Unexpected certificate or credential file.')
    return parts


def is_redirect(path):
    # OneDrive cloud placeholders are reparse points too, but not name surrogates.
    info = path.lstat()
    return path.is_symlink() or bool(getattr(info, 'st_reparse_tag', 0) & 0x20000000)


def safe_payload_path(name):
    safe_program_path(name)
    if name != 'StockAndSleeve.exe' and name != '_internal' and not name.startswith('_internal/'):
        raise PackageError('Unexpected top-level program payload.')
    if Path(name).suffix.casefold() == '.json' and name not in PROGRAM_JSON:
        raise PackageError('Unapproved JSON data in program payload.')


def validate_vendor_sboms(files):
    present = set(files) & VENDOR_SBOMS.keys()
    if not present:
        return
    if present != VENDOR_SBOMS.keys():
        raise PackageError('The vendor SBOM pair is incomplete.')
    try:
        distribution = metadata.distribution('cryptography')
    except metadata.PackageNotFoundError as error:
        raise PackageError('Install the pinned cryptography build distribution before packaging its SBOMs.') from error
    if distribution.version != CRYPTOGRAPHY_VERSION:
        raise PackageError('The installed cryptography distribution does not match the pinned SBOM version.')
    records = {str(entry).replace('\\', '/'): entry for entry in distribution.files or ()}
    for bundled, installed in VENDOR_SBOMS.items():
        record = records.get(installed)
        if record is None or record.hash is None or record.hash.mode != 'sha256' or record.size is None:
            raise PackageError('The pinned vendor SBOM is missing its installed RECORD integrity metadata.')
        original = Path(distribution.locate_file(record))
        if not original.is_file() or is_redirect(original):
            raise PackageError('The pinned vendor SBOM is missing or redirected.')
        hashed = digest_file(original)
        encoded = base64.urlsafe_b64encode(bytes.fromhex(hashed)).decode('ascii').rstrip('=')
        if encoded != record.hash.value or original.stat().st_size != record.size:
            raise PackageError('The installed vendor SBOM failed its RECORD integrity check.')
        if digest_file(files[bundled]) != hashed:
            raise PackageError('A bundled SBOM differs from the installed pinned vendor distribution.')


def program_files(root):
    root = Path(root)
    if not root.is_dir() or is_redirect(root):
        raise PackageError('Choose a regular standalone program directory.')
    files, seen = {}, set()

    def visit(folder):
        for entry in sorted(folder.iterdir()):
            name = entry.relative_to(root).as_posix()
            safe_payload_path(name)
            if is_redirect(entry):
                raise PackageError('Links and junctions cannot be packaged.')
            if entry.is_dir():
                visit(entry)
            elif entry.is_file():
                if name.casefold() in seen:
                    raise PackageError('Case-aliased payload paths.')
                seen.add(name.casefold())
                files[name] = entry
            else:
                raise PackageError('Only regular program files can be packaged.')
    visit(root)
    return files


def json_object(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise PackageError('Duplicate JSON field.')
            result[key] = value
        return result
    try:
        result = json.loads(raw, object_pairs_hook=unique)
    except (ValueError, UnicodeError) as error:
        raise PackageError('Invalid build record JSON.') from error
    if not isinstance(result, dict):
        raise PackageError('Build record must be a JSON object.')
    return result


def validate_build(files, source_root):
    required = {'StockAndSleeve.exe', '_internal/desktop_build.json',
                '_internal/assets/stock-and-sleeve.ico'}
    if not required.issubset(files):
        raise PackageError('Build the complete current desktop program with build identity and icon.')
    metadata = json_object(files['_internal/desktop_build.json'].read_text(encoding='utf-8'))
    if type(metadata.get('format_version')) is not int or metadata['format_version'] != 1 or metadata.get('channel') != 'private-pilot':
        raise PackageError('The build is not a supported private pilot.')
    inputs = metadata.get('input_sha256')
    if not isinstance(inputs, dict) or not inputs:
        raise PackageError('Missing source-input record.')
    for name, hashed in inputs.items():
        safe_program_path(name)
        if not isinstance(hashed, str) or not re.fullmatch('[a-f0-9]{64}', hashed):
            raise PackageError('Invalid source hash record.')
    build_id = hashlib.sha256(json.dumps(inputs, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if metadata.get('build_id') != build_id:
        raise PackageError('Build identity does not match its source-input record.')
    current = build_input_hashes(source_root)
    if inputs != current:
        raise PackageError('Build inputs differ from selected source. Rebuild from stable sources.')
    for name, hashed in inputs.items():
        if Path(name).suffix != '.py':
            bundled = '_internal/' + name
            if bundled not in files or digest_file(files[bundled]) != hashed:
                raise PackageError('A bundled asset differs from the build source record.')
    return build_id, current


def readme(version, build_id, evidence):
    return f'''Stock & Sleeve {version} SHARED PILOT (unsigned)

Extract the entire ZIP, then double-click Install Shared Pilot.cmd.
Open Stock & Sleeve Pilot on your Desktop or Start Menu. Existing shortcuts keep
their targets; a new installation adds its own suffix if the name is occupied.
Use Stop Stock & Sleeve Pilot for this installation, or Quit inside the app.
No Python installation is required; Chrome is required for the desktop window.

Hashes are checked before and after copying. Each installation gets a unique
pilot profile and its own available persisted loopback port. No business data
is copied or migrated; other installations and the old Test shortcut stay in
place. Installation does not start the app. Start/Stop use the same profile/port,
recorded in pilot-install.json. If the port later becomes busy, resolve the
conflict; do not point the pilot at another profile.

AVAILABLE: reviewed intake/manual cards/slabs, game/scanner adapters, labels,
pricing research and invited online shared customer-credit accounts/history and
manager corrections. Shared credit requires internet and an invited staff login;
uncertain writes remain in a protected recovery journal.
GATED: Square OAuth requires securely configured sandbox credentials and merchant
consent. Checkout, tracked-discount automatic redemption and hosted accepted-trade
issuance require final server/sandbox/device/tax/report acceptance; related source
is under development. Do not use the pilot for production credit redemption.
CLEANUP: conservative review/dry run; live deletion remains gated.
Commercial privacy, full rights/offboarding, retention, restore, release signing,
provider rights and Marketplace acceptance remain open release gates.

No production credentials or merchant data belong in this package. Local records
are not fully application-encrypted; Windows disk/account controls still matter.
Uninstall/reinstall is not a rights-deletion workflow. Preserve uncertain
financial journals until their outcomes have been verified.

Build identity: {build_id}
Included evidence: {', '.join(evidence) if evidence else 'none selected'}.
Only exact reviewed/requested evidence is included. Its dates and source hashes
define its scope; tests do not establish universal consumer readiness, live Square,
hardware, tax, security or usability acceptance. SHA256 detects changed files
against this supplied record; it does not replace a publisher signature. Verify
the separate download checksum through trusted delivery.
'''


def verify_archive(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(n.casefold() for n in names)) or archive.testzip():
            raise PackageError('Duplicate archive entries or failed ZIP integrity.')
        program = json_object(archive.read('ProgramSHA256.json'))
        evidence = json_object(archive.read('EvidenceSHA256.json'))
        expected = set(TOP_FILES)
        for name, hashed in program.items():
            safe_payload_path(name)
            target = 'StockAndSleeve/' + name
            expected.add(target)
            if hashlib.sha256(archive.read(target)).hexdigest() != hashed:
                raise PackageError('Archived program file changed.')
        for name, hashed in evidence.items():
            safe_relative(name)
            if name not in EVIDENCE_ALLOWLIST:
                raise PackageError('Unreviewed evidence.')
            target = 'Test results/' + name
            expected.add(target)
            if hashlib.sha256(archive.read(target)).hexdigest() != hashed:
                raise PackageError('Archived evidence changed.')
        if set(names) != expected:
            raise PackageError('Unexpected or missing archive files.')
        return {'program_files': len(program), 'evidence_files': len(evidence)}


def package_preview(program_directory, output_path, *, source_root=ROOT,
                    evidence_directory=None, evidence=(), version='2.6.0'):
    if not re.fullmatch(r'[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9-]+)?', version):
        raise PackageError('Use a simple release version.')
    candidate = Path(program_directory).absolute()
    if candidate.exists() and is_redirect(candidate):
        raise PackageError('Linked program roots are not allowed.')
    source, output = candidate.resolve(), Path(output_path).resolve()
    if output.suffix.casefold() != '.zip' or output == source or source in output.parents:
        raise PackageError('Choose an explicit ZIP outside the program directory.')
    if output.exists() or output.with_suffix('.sha256').exists():
        raise PackageError('Output exists. Choose a fresh reviewed artifact name.')
    files = program_files(source)
    build_id, inputs = validate_build(files, Path(source_root))
    validate_vendor_sboms(files)
    hashes = {name: digest_file(file) for name, file in files.items()}
    selected = {}
    if evidence_directory is not None and is_redirect(Path(evidence_directory)):
        raise PackageError('Linked evidence directories are not allowed.')
    for name in evidence:
        safe_relative(name)
        if name not in EVIDENCE_ALLOWLIST or name in selected:
            raise PackageError('Select each evidence filename once from the reviewed allowlist.')
        if evidence_directory is None:
            raise PackageError('Select an evidence directory explicitly.')
        file = Path(evidence_directory) / name
        if not file.is_file() or is_redirect(file):
            raise PackageError('Requested evidence is missing or linked.')
        selected[name] = file
    evidence_hashes = {name: digest_file(file) for name, file in selected.items()}
    installer = (ROOT / 'tools' / 'pilot_installer.ps1').read_text(encoding='utf-8')
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(output.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with zipfile.ZipFile(temporary, 'x', zipfile.ZIP_DEFLATED) as archive:
            for name, file in files.items():
                archive.write(file, 'StockAndSleeve/' + name)
            for name, file in selected.items():
                archive.write(file, 'Test results/' + name)
            archive.writestr('ProgramSHA256.json', json.dumps(hashes, indent=2) + '\n')
            archive.writestr('EvidenceSHA256.json', json.dumps(evidence_hashes, indent=2) + '\n')
            archive.writestr('package.json', json.dumps({'kind': 'shared-pilot', 'version': version,
                              'build_id': build_id, 'schema': 1}, indent=2) + '\n')
            archive.writestr('Install Shared Pilot.ps1', installer)
            archive.writestr('Install Shared Pilot.cmd', '@echo off\r\ncd /d "%~dp0"\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "Install Shared Pilot.ps1"\r\nset "pilot_exit=%errorlevel%"\r\npause\r\nexit /b %pilot_exit%\r\n')
            archive.writestr('READ ME FIRST.txt', readme(version, build_id, selected))
        after = program_files(source)
        if set(after) != set(files) or any(digest_file(after[n]) != h for n, h in hashes.items()):
            raise PackageError('Program changed during packaging. Rebuild and retry.')
        validate_vendor_sboms(after)
        if any(digest_file(selected[n]) != h for n, h in evidence_hashes.items()):
            raise PackageError('Evidence changed during packaging. Review and retry.')
        if build_input_hashes(source_root) != inputs:
            raise PackageError('Source changed during packaging. Rebuild and retry.')
        counts = verify_archive(temporary)
        with output.open('xb') as target, temporary.open('rb') as original:
            for chunk in iter(lambda: original.read(1024 * 1024), b''):
                target.write(chunk)
        if digest_file(output) != digest_file(temporary):
            raise PackageError('Delivered archive changed while copying.')
        with output.with_suffix('.sha256').open('x', encoding='ascii') as checksum:
            checksum.write(digest_file(output) + '  ' + output.name + '\n')
        return {'archive': str(output), 'sha256': digest_file(output), 'build_id': build_id, **counts}
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--program-directory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--source-root', type=Path, default=ROOT)
    parser.add_argument('--evidence-directory', type=Path)
    parser.add_argument('--evidence', action='append', default=[], choices=sorted(EVIDENCE_ALLOWLIST))
    parser.add_argument('--version', default='2.6.0')
    args = parser.parse_args()
    try:
        result = package_preview(args.program_directory, args.output, source_root=args.source_root,
                                 evidence_directory=args.evidence_directory, evidence=args.evidence,
                                 version=args.version)
    except (OSError, PackageError, zipfile.BadZipFile) as error:
        parser.exit(1, 'Package rejected: ' + str(error) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
