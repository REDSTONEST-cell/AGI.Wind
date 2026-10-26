"""Reuse the authorized NAS AliDNS config without printing or saving plaintext keys."""
import base64
import ctypes
import datetime
import getpass
import hashlib
import hmac
import json
from pathlib import Path
import socket
import sys
import urllib.parse
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / '.devtools/https-python'))
import paramiko
import yaml


class Blob(ctypes.Structure):
    _fields_ = [('size', ctypes.c_ulong), ('data', ctypes.POINTER(ctypes.c_byte))]


def protect(data):
    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    result = Blob()
    if not ctypes.windll.crypt32.CryptProtectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(result)):
        raise RuntimeError('Windows credential protection failed')
    try:
        return ctypes.string_at(result.data, result.size)
    finally:
        ctypes.windll.kernel32.LocalFree(result.data)


def rpc(credentials, action, **values):
    values.update(Action=action, Format='JSON', Version='2015-01-09', AccessKeyId=credentials['id'], SignatureMethod='HMAC-SHA1', SignatureVersion='1.0', SignatureNonce=str(uuid.uuid4()), Timestamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'))
    quote = lambda value: urllib.parse.quote(str(value), safe='~')
    query = '&'.join(quote(key) + '=' + quote(value) for key, value in sorted(values.items()))
    signature = base64.b64encode(hmac.new((credentials['secret'] + '&').encode(), ('GET&%2F&' + quote(query)).encode(), hashlib.sha1).digest()).decode()
    url = 'https://alidns.aliyuncs.com/?' + query + '&Signature=' + quote(signature)
    try:
        with urllib.request.urlopen(url, timeout=20) as response:
            result = json.load(response)
    except Exception:
        # Do not print an exception containing the signed URL or access key ID.
        raise RuntimeError('AliDNS request failed; credentials and signed URL withheld') from None
    if 'Code' in result:
        raise RuntimeError('AliDNS rejected operation: ' + str(result.get('Code', 'unknown')))
    return result


def main():
    config_file = ROOT / 'config/https.local.json'
    config = json.loads(config_file.read_text(encoding='utf8'))
    address = config['bind_address']
    password = getpass.getpass('NAS SSH password (hidden): ')
    client = paramiko.SSHClient()
    client.load_host_keys(str(Path.home() / '.ssh/known_hosts'))
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    try:
        client.connect('192.168.8.213', username='wind_shake', password=password, timeout=8, auth_timeout=8, look_for_keys=False, allow_agent=False)
        stdin, stdout, stderr = client.exec_command("sudo -S -p '' cat /opt/ddns-go/.ddns_go_config.yaml", timeout=10)
        stdin.write(password + '\n')
        stdin.flush()
        stdin.channel.shutdown_write()
        raw = stdout.read()
        status = stdout.channel.recv_exit_status()
        if status:
            raise RuntimeError('NAS configuration could not be read; SSH/sudo authorization required')
        document = yaml.safe_load(raw)
    finally:
        client.close()
        del password
    entries = [entry.get('dns', {}) for entry in document.get('dnsconf', [])]
    matches = [entry for entry in entries if str(entry.get('name', '')).lower() in ('alidns', 'aliyun') and entry.get('id') and entry.get('secret')]
    if len(matches) != 1:
        raise RuntimeError('Expected exactly one configured AliDNS account; no keys displayed')
    credentials = {'id': str(matches[0]['id']), 'secret': str(matches[0]['secret'])}
    domain, rr = 'weidu123526.top', 'agi'
    result = rpc(credentials, 'DescribeDomainRecords', DomainName=domain, RRKeyWord=rr, PageSize=100)
    records = [record for record in result.get('DomainRecords', {}).get('Record', []) if record.get('RR') == rr]
    if records:
        if len(records) != 1 or records[0].get('Type') != 'AAAA' or socket.inet_pton(socket.AF_INET6, records[0]['Value']) != socket.inet_pton(socket.AF_INET6, address):
            raise RuntimeError('agi subdomain already has a different record; existing DNS was preserved')
        record_id = records[0]['RecordId']
    else:
        record_id = rpc(credentials, 'AddDomainRecord', DomainName=domain, RR=rr, Type='AAAA', Value=address, TTL=600)['RecordId']
    state = ROOT / 'data/https'
    state.mkdir(parents=True, exist_ok=True)
    (state / 'alidns.dpapi').write_bytes(protect(json.dumps(credentials).encode()))
    (state / 'dns-record.json').write_text(json.dumps({'domain': domain, 'rr': rr, 'record_id': record_id, 'address': address}), encoding='utf8')
    config.update(hostname=rr + '.' + domain, https_port=18443, dns_provider='alidns')
    config_file.write_text(json.dumps(config, indent=2) + '\n', encoding='utf8')
    print('AliDNS AAAA configured for agi.weidu123526.top; original NAS records preserved.')
    print('DNS credentials protected with Windows current-user DPAPI; no plaintext key file created.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print('Provisioning stopped:', str(error) if isinstance(error, RuntimeError) else type(error).__name__)
        sys.exit(1)
