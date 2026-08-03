import { Button, Checkbox, Modal, Spin, Tag, Toast } from "@douyinfe/semi-ui";
import { FileSearch, Upload } from "lucide-react";
import { useMemo, useRef, useState } from "react";
import { showApiError } from "../../shared/api/feedback";
import { commitScanReportImport, previewScanReportImport } from "../../shared/api/workProjects";
import type { ScanReportAssetCandidate, ScanReportImportPreview } from "../../shared/api/types";
import { WORK_PROJECT_ASSET_TYPE_LABEL } from "../../shared/lib/labels";

type ScanReportImportModalProps = {
  visible: boolean;
  projectId: number;
  onClose: () => void;
  onImported: () => void;
};

const MAX_VISIBLE_ASSETS = 1000;

export function ScanReportImportModal({ visible, projectId, onClose, onImported }: ScanReportImportModalProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const [file, setFile] = useState<File | null>(null);
  const [preview, setPreview] = useState<ScanReportImportPreview | null>(null);
  const [selectedKeys, setSelectedKeys] = useState<Set<string>>(new Set());
  const [createRelationships, setCreateRelationships] = useState(true);
  const [createBlackboardFact, setCreateBlackboardFact] = useState(true);
  const [busy, setBusy] = useState(false);

  const duplicateKeys = useMemo(() => new Set(preview?.duplicate_keys ?? []), [preview]);
  const visibleAssets = preview?.assets.slice(0, MAX_VISIBLE_ASSETS) ?? [];
  const allSelected = Boolean(preview?.assets.length) && selectedKeys.size === preview?.assets.length;
  const someSelected = selectedKeys.size > 0 && !allSelected;

  const reset = () => {
    setFile(null);
    setPreview(null);
    setSelectedKeys(new Set());
    setCreateRelationships(true);
    setCreateBlackboardFact(true);
    setBusy(false);
  };

  const close = () => {
    if (busy) return;
    reset();
    onClose();
  };

  const chooseFile = (nextFile: File | null) => {
    setFile(nextFile);
    setPreview(null);
    setSelectedKeys(new Set());
  };

  const analyze = async () => {
    if (!file) return;
    setBusy(true);
    try {
      const response = await previewScanReportImport(projectId, file);
      if (!response.data) throw new Error("Scan report preview is empty");
      setPreview(response.data);
      setSelectedKeys(new Set(response.data.assets.map((asset) => asset.key)));
    } catch (error) {
      showApiError(error);
    } finally {
      setBusy(false);
    }
  };

  const commit = async () => {
    if (!preview || selectedKeys.size === 0) return;
    setBusy(true);
    try {
      const allAssetsSelected = selectedKeys.size === preview.assets.length;
      const response = await commitScanReportImport(projectId, preview.import_info.import_id, {
        selected_asset_keys: allAssetsSelected ? null : Array.from(selectedKeys),
        create_relationships: createRelationships,
        create_blackboard_fact: createBlackboardFact,
      });
      const result = response.data;
      Toast.success(
        result
          ? `Imported ${result.created_assets.length} new and updated ${result.updated_assets.length} assets`
          : "Scan report imported",
      );
      onImported();
      reset();
      onClose();
    } catch (error) {
      showApiError(error);
      setBusy(false);
    }
  };

  const toggleAsset = (asset: ScanReportAssetCandidate, checked: boolean) => {
    setSelectedKeys((current) => {
      const next = new Set(current);
      if (checked) next.add(asset.key);
      else next.delete(asset.key);
      return next;
    });
  };

  const toggleAll = (checked: boolean) => {
    setSelectedKeys(new Set(checked ? preview?.assets.map((asset) => asset.key) ?? [] : []));
  };

  return (
    <Modal
      visible={visible}
      title="Import Nmap report"
      width="min(760px, calc(100vw - 24px))"
      okText={preview ? "Import selected" : "Analyze"}
      cancelText="Cancel"
      confirmLoading={busy}
      okButtonProps={{ disabled: preview ? selectedKeys.size === 0 : !file }}
      onOk={() => void (preview ? commit() : analyze())}
      onCancel={close}
      closeOnEsc={!busy}
      maskClosable={!busy}
    >
      <Spin spinning={busy}>
        <div className="scan-report-import">
          <input
            ref={inputRef}
            type="file"
            accept=".xml,application/xml,text/xml"
            hidden
            onChange={(event) => {
              chooseFile(event.target.files?.[0] ?? null);
              event.target.value = "";
            }}
          />
          <div className="scan-report-file-row">
            <div>
              <FileSearch size={18} />
              <span>{file?.name ?? "No report selected"}</span>
              {file ? <small>{formatBytes(file.size)}</small> : null}
            </div>
            <Button icon={<Upload size={14} />} theme="solid" type="tertiary" onClick={() => inputRef.current?.click()} disabled={busy}>
              Choose XML
            </Button>
          </div>

          {preview ? (
            <>
              <div className="scan-report-counts">
                <Count label="Hosts" value={preview.counts.hosts} />
                <Count label="Networks" value={preview.counts.networks} />
                <Count label="Domains" value={preview.counts.domains} />
                <Count label="Services" value={preview.counts.services} />
              </div>
              <div className="scan-report-options">
                <Checkbox checked={createRelationships} onChange={(event) => setCreateRelationships(Boolean(event.target.checked))}>
                  Create relationships
                </Checkbox>
                <Checkbox checked={createBlackboardFact} onChange={(event) => setCreateBlackboardFact(Boolean(event.target.checked))}>
                  Add blackboard summary
                </Checkbox>
              </div>
              <div className="scan-report-selection-header">
                <Checkbox
                  checked={allSelected}
                  indeterminate={someSelected}
                  onChange={(event) => toggleAll(Boolean(event.target.checked))}
                >
                  {selectedKeys.size} of {preview.assets.length} assets
                </Checkbox>
                {preview.warnings.length ? <Tag color="amber">{preview.warnings.length} warnings</Tag> : null}
              </div>
              {preview.assets.length > MAX_VISIBLE_ASSETS ? (
                <div className="scan-report-limit">Showing the first {MAX_VISIBLE_ASSETS.toLocaleString()} candidates.</div>
              ) : null}
              <div className="scan-report-assets">
                {visibleAssets.map((asset) => (
                  <label key={asset.key} className="scan-report-asset-row">
                    <Checkbox checked={selectedKeys.has(asset.key)} onChange={(event) => toggleAsset(asset, Boolean(event.target.checked))} />
                    <span>
                      <strong>{asset.identifier}</strong>
                      {asset.extra?.service_name ? <small>{asset.extra.service_name}</small> : null}
                    </span>
                    <Tag>{WORK_PROJECT_ASSET_TYPE_LABEL[asset.type]}</Tag>
                    {duplicateKeys.has(asset.key) ? <Tag color="blue">Existing</Tag> : null}
                  </label>
                ))}
              </div>
            </>
          ) : null}
        </div>
      </Spin>
    </Modal>
  );
}

function Count({ label, value }: { label: string; value: number }) {
  return <div><strong>{value}</strong><span>{label}</span></div>;
}

function formatBytes(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
