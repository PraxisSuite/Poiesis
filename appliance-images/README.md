[← Back to README](../README.md)

# appliance-images/

This directory holds **licensed vendor appliance images** that Poiesis uploads to Proxmox at
deploy time. Today this is BIG-IP qcow2 files; the same directory layout supports any future
appliance type.

The directory is gitignored — qcow images are large (gigabytes) and licensed, so they must
never be committed. This README is the one exception (`!appliance-images/README.md` in
`.gitignore`).

---

## What to put here

Drop the vendor image directly into this directory. Filename matters for auto-discovery:

| Appliance | Filename pattern | Notes |
|---|---|---|
| F5 BIG-IP | `BIGIP*.qcow2` | The licensed qcow you downloaded from F5 |
| F5 BIG-IP (compressed) | `BIGIP*.qcow2.zip` | Will be auto-extracted on first use |

Examples:
```
appliance-images/BIGIP-21.1.0-0.0.38.qcow2
appliance-images/BIGIP-17.5.0-0.0.13.ALL.qcow2.zip
```

---

## How to get an image here

### F5 BIG-IP

1. Log into [downloads.f5.com](https://downloads.f5.com) with your F5 support account.
2. Navigate to **BIG-IP** → choose a version → **Virtual Edition** → pick the **qcow2** image
   (NOT the OVA — that's for ESXi/vCenter).
3. Download the `.qcow2` file (or the `.zip` containing it) and place it directly in this
   directory.

Once the file is present, you don't have to extract anything — Poiesis handles `.qcow2.zip`
automatically on the first deploy that references it.

---

## How Poiesis uses these images

Referenced from a deployment file:

```json
{
  "type": "bigip",
  "qcow_filename": "BIGIP-21.1.0-0.0.38.qcow2",
  ...
}
```

On `python3 deploy.py --deploy-file deployments/bigip/your-bigip.json` (or
`deploy_bigip.py` directly), Poiesis will:

1. **Resolve** the qcow:
   - If the named `.qcow2` is here directly → use it.
   - If only a matching `.qcow2.zip` is here → unzip it in place, **delete the .zip on
     success**, and use the extracted `.qcow2`.
   - If a future redeploy references the same name, the already-extracted file is reused.
2. **SFTP** the qcow to the target Proxmox node's `/tmp/` directory (~8 GB transfer per
   deploy — runs in parallel across nodes in batch mode).
3. **`qm importdisk`** on the Proxmox node to materialize the disk on your chosen storage
   (e.g. `local-lvm`).
4. **Attach** the imported disk as `scsi0`, resize to `disk_gb`, configure the VM's NICs from
   the deployment file's `nics[]` array.
5. **Clean up** the staged copy in `/tmp/` on the Proxmox node.

Subsequent deploys of the same image to other nodes do not require you to re-download —
Poiesis re-uploads from this local copy as needed.

---

## Disk space

Each extracted BIG-IP `.qcow2` is ~8 GB. If you have multiple versions staged here, plan
accordingly. Old images you no longer deploy from can be deleted safely — the deployment
JSON references the filename, so as long as your active deployment files point at images
still present, you're fine.

---

[← Back to README](../README.md)
