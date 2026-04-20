import axios from 'axios';
import { BACKEND_URL } from '../utils/constants';

const client = axios.create({
  baseURL: BACKEND_URL,
  timeout: 15_000,
  headers: { 'Content-Type': 'application/json' },
});

// Log errors in dev
client.interceptors.response.use(
  (res) => res,
  (err) => {
    if (__DEV__) {
      console.error('[API Error]', err.response?.status, err.config?.url, err.response?.data);
    }
    return Promise.reject(err);
  },
);

export default client;
