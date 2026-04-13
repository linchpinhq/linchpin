import LoadingSpinner from './LoadingSpinner';
import './Pagination.css';

interface PaginationProps {
  hasMore: boolean;
  onLoadMore: () => void;
  loading?: boolean;
}

export default function Pagination({ hasMore, onLoadMore, loading = false }: PaginationProps) {
  if (!hasMore) return null;

  return (
    <div className="pagination" data-testid="pagination">
      <button
        className="pagination-load-more"
        onClick={onLoadMore}
        disabled={loading}
        data-testid="load-more"
      >
        {loading ? <LoadingSpinner inline /> : 'Load more'}
      </button>
    </div>
  );
}
