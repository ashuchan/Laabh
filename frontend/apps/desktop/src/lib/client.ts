import { createApiClient } from '@laabh/shared';

// In dev, Vite proxies to localhost:8000; in prod, same-origin from FastAPI static mount
const BACKEND_URL = import.meta.env.VITE_BACKEND_URL ?? '';

export const apiClient = createApiClient(BACKEND_URL);
export const WS_URL = (BACKEND_URL || window.location.origin).replace(/^http/, 'ws') + '/ws/prices';
