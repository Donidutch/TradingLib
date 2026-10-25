import { useState, type ReactNode } from "react";
import { Button } from "./button";
import { Modal } from "./modal";

/**
 * Ask before a destructive action (delete, reject, cancel). The action runs
 * only on the confirm button; Escape, the backdrop and Cancel all back out.
 */
export function ConfirmDialog({
  open,
  title,
  children,
  confirmLabel,
  onConfirm,
  onCancel,
}: {
  open: boolean;
  title: ReactNode;
  children?: ReactNode;
  confirmLabel: string;
  onConfirm: () => void | Promise<void>;
  onCancel: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const confirm = async () => {
    setBusy(true);
    try {
      await onConfirm();
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onCancel}
      title={title}
      footer={
        <>
          <Button size="sm" onClick={onCancel} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" variant="danger" onClick={() => void confirm()} disabled={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      {children}
    </Modal>
  );
}
