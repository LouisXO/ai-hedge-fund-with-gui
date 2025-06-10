// API Configuration
const getApiBaseUrl = (): string => {
  // Check if we're in development mode
  if (import.meta.env.DEV) {
    return 'http://localhost:8000';
  }
  
  // For production, use the same host as the frontend but on port 8000
  // You can also use environment variables or different logic here
  const protocol = window.location.protocol;
  const hostname = window.location.hostname;
  
  // If hostname is localhost in production (unlikely but possible)
  if (hostname === 'localhost' || hostname === '127.0.0.1') {
    return 'http://localhost:8000';
  }
  
  // For production deployment, construct API URL
  // Option 1: Same domain, different port
  return `${protocol}//${hostname}:8000`;
  
  // Option 2: Subdomain approach (uncomment if using subdomains)
  // return `${protocol}//api.${hostname}`;
  
  // Option 3: Different domain (uncomment and modify if API is on different domain)
  // return 'https://your-api-domain.com';
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