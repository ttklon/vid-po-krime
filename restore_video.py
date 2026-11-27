#!/usr/bin/env python3
"""Restore original videos from checked and ordered Git parts, without loading them into RAM."""
import argparse
import hashlib
import json
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def restore(manifest_path, output_dir):
    manifest = json.loads(Path(manifest_path).read_text())
    name = manifest['original_name'].replace('\\', '/').split('/')[-1]
    if name in ('', '.', '..'):
        raise ValueError('Invalid output filename')
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    dest = output_dir / name
    if dest.exists():
        raise FileExistsError('Refusing to overwrite ' + str(dest))
    tmp = output_dir / (name + '.assembling')
    digest, size = hashlib.sha256(), 0
    try:
        with tmp.open('xb') as out:
            for part in manifest['parts']:
                path = (ROOT / part['path']).resolve()
                if not path.is_relative_to((ROOT / 'media').resolve()):
                    raise ValueError('Part path outside media directory')
                part_hash, part_size = hashlib.sha256(), 0
                with path.open('rb') as stream:
                    for block in iter(lambda: stream.read(4 * 1024**2), b''):
                        out.write(block)
                        digest.update(block)
                        part_hash.update(block)
                        size += len(block)
                        part_size += len(block)
                if part_hash.hexdigest() != part['sha256'] or part_size != part['size']:
                    raise ValueError('Part checksum mismatch: ' + part['path'])
            if digest.hexdigest() != manifest['sha256'] or size != manifest['size']:
                raise ValueError('Full video checksum mismatch')
            out.flush()
            os.fsync(out.fileno())
        # Hard-link creation is atomic and refuses to overwrite an existing destination.
        os.link(tmp, dest)
    finally:
        # Do not remove a pre-existing temporary file we did not create.
        if 'out' in locals():
            tmp.unlink(missing_ok=True)
    return dest


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifests', nargs='+', help='videos/*.json')
    parser.add_argument('--output', default='restored')
    args = parser.parse_args()
    for manifest in args.manifests:
        print('SHA-256 OK:', restore(manifest, args.output))
