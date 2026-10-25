import {execFileSync} from 'node:child_process';
import {resolve} from 'node:path';
execFileSync(resolve(process.platform === 'win32' ? 'app/.venv/Scripts/python.exe' : 'app/.venv/bin/python'), ['scripts/generate-contracts.py'], {stdio:'inherit'});
const {compile} = await import('json-schema-to-typescript');
const {readFileSync, writeFileSync} = await import('node:fs');
writeFileSync('packages/contracts/types.ts', await compile(JSON.parse(readFileSync('packages/contracts/schema.json','utf8')), 'AGIWindProtocol10', {additionalProperties:false, unreachableDefinitions:true}));
