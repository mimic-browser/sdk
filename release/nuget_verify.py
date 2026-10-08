"""Verify NuGet.org repository signing without changing the packed SDK payload."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET
import zipfile


SERVICE_INDEX = "https://api.nuget.org/v3/index.json"
SHA256_OID = "2.16.840.1.101.3.4.2.1"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def repository_certificates(fetch_json):
    index = fetch_json(SERVICE_INDEX)
    resources = [entry for entry in index.get("resources", [])
                 if entry.get("@type") == "RepositorySignatures/5.0.0"]
    if len(resources) != 1:
        raise ValueError("NuGet.org must advertise one repository signature resource")
    url = resources[0].get("@id", "")
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.hostname != "api.nuget.org"
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise ValueError("NuGet.org repository signature metadata must use its HTTPS origin")
    metadata = fetch_json(url)
    if metadata.get("allRepositorySigned") is not True:
        raise ValueError("NuGet.org repository signing requirement is absent")
    fingerprints = []
    for certificate in metadata.get("signingCertificates", []):
        fingerprint = certificate.get("fingerprints", {}).get(SHA256_OID, "")
        if not isinstance(fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("NuGet.org advertised an invalid SHA256 certificate fingerprint")
        fingerprints.append(fingerprint)
    if not fingerprints:
        raise ValueError("NuGet.org advertised no trusted repository certificates")
    return url, metadata, sorted(set(fingerprints))


def entries(path):
    with zipfile.ZipFile(path) as archive:
        result = {}
        for entry in archive.infolist():
            if entry.filename != entry.orig_filename or entry.filename in result:
                raise ValueError("NuGet package contains an ambiguous or duplicate ZIP entry")
            result[entry.filename] = archive.read(entry)
        return result


def verify(unsigned, signed, *, fetch_json, inspect_archive):
    """Require identical original entries plus one valid NuGet.org signature."""
    unsigned, signed = Path(unsigned), Path(signed)
    original_inspection = inspect_archive(unsigned, "nuget")
    inspect_archive(signed, "nuget")
    original, downloaded = entries(unsigned), entries(signed)
    if ".signature.p7s" in original:
        raise ValueError("Expected the retained unsigned NuGet artifact")
    signature = downloaded.pop(".signature.p7s", None)
    if not signature:
        raise ValueError("NuGet registry artifact has no repository signature")
    if downloaded != original:
        raise ValueError("NuGet registry payload differs from the retained unsigned artifact")
    url, metadata, fingerprints = repository_certificates(fetch_json)
    with tempfile.TemporaryDirectory(prefix="mimic-nuget-verify-") as temporary:
        config = ET.Element("configuration")
        settings = ET.SubElement(config, "config")
        ET.SubElement(settings, "add", key="signatureValidationMode", value="require")
        signers = ET.SubElement(config, "trustedSigners")
        ET.SubElement(signers, "clear")
        repository = ET.SubElement(signers, "repository", name="nuget.org", serviceIndex=SERVICE_INDEX)
        for fingerprint in fingerprints:
            ET.SubElement(repository, "certificate", fingerprint=fingerprint,
                          hashAlgorithm="SHA256", allowUntrustedRoot="false")
        config_path = Path(temporary) / "NuGet.Config"
        ET.ElementTree(config).write(config_path, encoding="utf-8", xml_declaration=True)
        command = [os.getenv("MIMIC_RELEASE_DOTNET", "dotnet"), "nuget", "verify", str(signed),
                   "--all", "--configfile", str(config_path), "--verbosity", "minimal"]
        for fingerprint in fingerprints:
            command.extend(["--certificate-fingerprint", fingerprint])
        result = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=120,
                                env={**os.environ, "DOTNET_CLI_UI_LANGUAGE": "en-US"})
    diagnostic = (result.stdout + result.stderr).strip()
    if result.returncode:
        raise ValueError("NuGet repository signature verification failed: " + diagnostic[-16384:])
    match = re.search(r"Signature type: Repository\s+.*?SHA256 hash:\s*([0-9A-Fa-f]{64})",
                      diagnostic, re.DOTALL)
    if not match or match[1].lower() not in fingerprints:
        raise ValueError("NuGet verifier did not confirm an advertised repository signer")
    return {
        "unsignedSha256": sha(unsigned.read_bytes()),
        "downloadSha256": sha(signed.read_bytes()),
        "payloadSha256": original_inspection["contentSha256"],
        "payloadEntries": len(original),
        "addedEntries": [".signature.p7s"],
        "signatureSha256": sha(signature),
        "serviceIndex": SERVICE_INDEX,
        "repositorySignaturesUrl": url,
        "repositoryMetadataSha256": sha(json.dumps(metadata, sort_keys=True).encode()),
        "trustedCertificateFingerprints": fingerprints,
        "signerCertificateFingerprint": match[1].lower(),
        "signatureVerification": "dotnet nuget verify --all",
        "signatureVerificationOutput": diagnostic,
    }
