import './ConfirmDialog.css';

interface ConfirmDialogProps {
  title: string;
  message: string;
  onConfirm: () => void;
  onCancel: () => void;
  open: boolean;
}

export default function ConfirmDialog({ title, message, onConfirm, onCancel, open }: ConfirmDialogProps) {
  if (!open) return null;

  return (
    <div className="confirm-overlay" data-testid="confirm-overlay" onClick={onCancel}>
      <div
        className="confirm-dialog"
        data-testid="confirm-dialog"
        role="dialog"
        aria-labelledby="confirm-title"
        aria-describedby="confirm-message"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="confirm-dialog-title" id="confirm-title">{title}</div>
        <div className="confirm-dialog-message" id="confirm-message">{message}</div>
        <div className="confirm-dialog-actions">
          <button
            className="confirm-dialog-cancel"
            onClick={onCancel}
            data-testid="confirm-cancel"
          >
            Cancel
          </button>
          <button
            className="confirm-dialog-confirm"
            onClick={onConfirm}
            data-testid="confirm-confirm"
          >
            Confirm
          </button>
        </div>
      </div>
    </div>
  );
}
