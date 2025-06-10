// API Configuration
const getApiBaseUrl = (): string => {
  // Check if we're in development mode
  if (import.meta.env.DEV) {
    return 'http://localhost:8000';
  }
  
  // Check for environment variable first (most flexible)
  if (import.meta.env.VITE_API_BASE_URL) {
    return import.meta.env.VITE_API_BASE_URL;
  }
  
  // For production deployment with Cloudflare Tunnel
  // Replace this with your actual Cloudflare tunnel domain
  const CLOUDFLARE_TUNNEL_DOMAIN = 'your-api-domain.your-domain.com';
  
  // Use HTTPS for Cloudflare tunnel
  return `https://${CLOUDFLARE_TUNNEL_DOMAIN}`;
  
  // Fallback options (uncomment if needed):
  
  // Option 2: Same domain, different port (if both on same server)
  // const protocol = window.location.protocol;
  // const hostname = window.location.hostname;
  // return `${protocol}//${hostname}:8000`;
  
  // Option 3: Subdomain approach
  // const protocol = window.location.protocol;
  // const hostname = window.location.hostname;
  // return `${protocol}//api.${hostname}`;
};

// API endpoints configuration
export const API_CONFIG = {
  BASE_URL: getApiBaseUrl(),
  ENDPOINTS: {
    // Portfolio endpoints
    PORTFOLIO: '/portfolio/',
    PORTFOLIO_CREATE: '/portfolio/create',
    PORTFOLIO_POSITIONS: '/portfolio/positions',
    PORTFOLIO_STOCK_DATA: '/portfolio/stock-data',
    PORTFOLIO_HOLDINGS_DATA: '/portfolio/holdings-data',
    
    // Hedge fund analysis endpoints
    HEDGE_FUND_RUN: '/hedge-fund/run',
    
    // Add other endpoints here as needed
  }
};

// Utility function to build full API URLs
export const buildApiUrl = (endpoint: string): string => {
  return `${API_CONFIG.BASE_URL}${endpoint}`;
};

// Utility function for making API requests with error handling
export const apiRequest = async (
  endpoint: string, 
  options: RequestInit = {}
): Promise<Response> => {
  const url = buildApiUrl(endpoint);
  
  try {
    const response = await fetch(url, {
      headers: {
        'Content-Type': 'application/json',
        ...options.headers,
      },
      ...options,
    });
    
    return response;
  } catch (error) {
    console.error(`API request failed for ${url}:`, error);
    throw error;
  }
};

export default API_CONFIG; 