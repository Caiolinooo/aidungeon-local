import React, { useEffect, useState } from 'react';
import { WifiOff, AlertTriangle } from 'lucide-react';
import { api } from '../api.js';

export default function OfflineBanner() {
  const [isOffline, setIsOffline] = useState(!navigator.onLine);
  const [backendUnreachable, setBackendUnreachable] = useState(false);

  useEffect(() => {
    const handleOnline = () => {
      setIsOffline(false);
      // Verify backend connectivity
      api.checkAuth()
        .then(() => setBackendUnreachable(false))
        .catch((e) => {
          if (e.status === 0) setBackendUnreachable(true);
        });
    };

    const handleOffline = () => {
      setIsOffline(true);
    };

    window.addEventListener('online', handleOnline);
    window.addEventListener('offline', handleOffline);

    // Initial check and periodic heartbeat every 20s
    const checkConnection = () => {
      if (!navigator.onLine) {
        setIsOffline(true);
        return;
      }
      api.checkAuth()
        .then(() => setBackendUnreachable(false))
        .catch((e) => {
          if (e.status === 0) {
            setBackendUnreachable(true);
          } else {
            setBackendUnreachable(false);
          }
        });
    };

    checkConnection();
    const interval = setInterval(checkConnection, 20000);

    return () => {
      window.removeEventListener('online', handleOnline);
      window.removeEventListener('offline', handleOffline);
      clearInterval(interval);
    };
  }, []);

  if (!isOffline && !backendUnreachable) return null;

  return (
    <div
      role="alert"
      className="bg-amber-500/90 text-zinc-950 px-4 py-2 text-xs font-semibold flex items-center justify-center gap-2 shadow-md relative z-50 backdrop-blur"
    >
      {isOffline ? <WifiOff className="w-4 h-4 shrink-0" /> : <AlertTriangle className="w-4 h-4 shrink-0" />}
      <span>Servidor fora do ar — modo offline. As mudanças não são sincronizadas.</span>
    </div>
  );
}
