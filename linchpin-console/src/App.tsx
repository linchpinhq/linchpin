import { useState } from 'react';
import { RouterProvider } from 'react-router-dom';
import { router } from './router';
import { getApiKey } from './api/client';
import ApiKeyInput from './components/ApiKeyInput';

function App() {
  const [hasKey, setHasKey] = useState(!!getApiKey());

  if (!hasKey) {
    return <ApiKeyInput onKeySet={() => setHasKey(true)} />;
  }

  return <RouterProvider router={router} />;
}

export default App;
