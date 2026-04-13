import './ErrorMessage.css';

interface ErrorMessageProps {
  status: number;
  message: string;
  onRetry?: () => void;
}

export default function ErrorMessage({ status, message, onRetry }: ErrorMessageProps) {
  return (
    <div className="error-message" data-testid="error-message" role="alert">
      <span className="error-message-status" data-testid="error-status">
        Error {status}
      </span>
      <span className="error-message-text" data-testid="error-text">
        {message}
      </span>
      {onRetry && (
        <button
          className="error-message-retry"
          onClick={onRetry}
          data-testid="error-retry"
        >
          Retry
        </button>
      )}
    </div>
  );
}
