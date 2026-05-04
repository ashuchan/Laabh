import axios, { type AxiosInstance } from 'axios';

export function createApiClient(baseURL: string): AxiosInstance {
  const client = axios.create({
    baseURL,
    timeout: 15_000,
    headers: { 'Content-Type': 'application/json' },
  });

  client.interceptors.response.use(
    (res) => res,
    (err) => {
      console.error('[API Error]', err.response?.status, err.config?.url, err.response?.data);
      return Promise.reject(err);
    },
  );

  return client;
}
