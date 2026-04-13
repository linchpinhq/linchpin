import './LoadingSpinner.css';

interface LoadingSpinnerProps {
  inline?: boolean;
}

export default function LoadingSpinner({ inline = false }: LoadingSpinnerProps) {
  if (inline) {
    return (
      <span className="loading-spinner-inline" data-testid="loading-spinner-inline">
        <span className="spinner spinner--small" />
      </span>
    );
  }

  return (
    <div className="loading-spinner-fullpage" data-testid="loading-spinner">
      <span className="spinner spinner--large" />
    </div>
  );
}
