import { useState, useEffect } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../ui/card';
import { PortfolioOverview } from './PortfolioOverview';
import { HoldingsTable } from './HoldingsTable';
import { PerformanceChart } from './PerformanceChart';
import { AlertsPanel } from './AlertsPanel';

interface PortfolioHolding {
  ticker: string;
  shares: number;
  averagePrice: number;
  currentPrice: number;
  totalValue: number;
  unrealizedPnL: number;
  unrealizedPnLPercent: number;
  weight: number;
}

interface PortfolioData {
  totalValue: number;
  cashBalance: number;
  totalInvested: number;
  totalPnL: number;
  totalPnLPercent: number;
  holdings: PortfolioHolding[];
  lastUpdated: string;
}

export function Portfolio() {
  const [portfolioData, setPortfolioData] = useState<PortfolioData | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [autoRefresh, setAutoRefresh] = useState(true);
  const [refreshInterval, setRefreshInterval] = useState(30); // seconds

  // Fetch portfolio data from API
  const fetchPortfolioData = async () => {
    setIsLoading(true);
    try {
      const response = await fetch('http://localhost:8000/api/v1/portfolio/');
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      const data = await response.json();
      setPortfolioData(data);
    } catch (error) {
      console.error('Error fetching portfolio data:', error);
      // Fallback to mock data if API fails
      const mockData: PortfolioData = {
        totalValue: 105420.50,
        cashBalance: 15420.50,
        totalInvested: 85000.00,
        totalPnL: 5420.50,
        totalPnLPercent: 6.38,
        lastUpdated: new Date().toISOString(),
        holdings: [
          {
            ticker: 'AAPL',
            shares: 50,
            averagePrice: 180.00,
            currentPrice: 185.25,
            totalValue: 9262.50,
            unrealizedPnL: 262.50,
            unrealizedPnLPercent: 2.92,
            weight: 8.78
          },
          {
            ticker: 'MSFT',
            shares: 75,
            averagePrice: 380.00,
            currentPrice: 390.80,
            totalValue: 29310.00,
            unrealizedPnL: 810.00,
            unrealizedPnLPercent: 2.84,
            weight: 27.80
          },
          {
            ticker: 'GOOGL',
            shares: 100,
            averagePrice: 140.00,
            currentPrice: 145.20,
            totalValue: 14520.00,
            unrealizedPnL: 520.00,
            unrealizedPnLPercent: 3.71,
            weight: 13.78
          },
          {
            ticker: 'TSLA',
            shares: 30,
            averagePrice: 220.00,
            currentPrice: 205.50,
            totalValue: 6165.00,
            unrealizedPnL: -435.00,
            unrealizedPnLPercent: -6.59,
            weight: 5.85
          }
        ]
      };
      setPortfolioData(mockData);
    } finally {
      setIsLoading(false);
    }
  };

  // Initial data fetch
  useEffect(() => {
    fetchPortfolioData();
  }, []);

  // Auto-refresh functionality
  useEffect(() => {
    if (!autoRefresh) return;

    const interval = setInterval(() => {
      fetchPortfolioData();
    }, refreshInterval * 1000);

    return () => clearInterval(interval);
  }, [autoRefresh, refreshInterval]);

  const handleRefreshIntervalChange = (newInterval: number) => {
    setRefreshInterval(newInterval);
  };

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">Portfolio Dashboard</h1>
          <p className="text-gray-600 dark:text-gray-400">
            Real-time portfolio monitoring and management
          </p>
        </div>
        
        {/* Controls */}
        <div className="flex items-center gap-4">
          <div className="flex items-center gap-2">
            <label className="text-sm text-gray-600">Auto-refresh</label>
            <input
              type="checkbox"
              checked={autoRefresh}
              onChange={(e) => setAutoRefresh(e.target.checked)}
              className="w-4 h-4"
            />
          </div>
          
          <select
            value={refreshInterval}
            onChange={(e) => handleRefreshIntervalChange(Number(e.target.value))}
            disabled={!autoRefresh}
            className="px-3 py-2 border rounded-md text-sm"
          >
            <option value={10}>10s</option>
            <option value={30}>30s</option>
            <option value={60}>1m</option>
            <option value={300}>5m</option>
          </select>

          <div className="flex items-center gap-2">
            <div className={`w-3 h-3 rounded-full ${isLoading ? 'bg-blue-500 animate-pulse' : 'bg-green-500'}`} />
            <span className="text-sm text-gray-600">
              {isLoading ? 'Updating...' : 'Live'}
            </span>
          </div>
        </div>
      </div>

      {portfolioData && (
        <>
          {/* Portfolio Overview */}
          <PortfolioOverview 
            data={portfolioData}
            isLoading={isLoading}
          />

          {/* Main Content Grid */}
          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            {/* Holdings Table */}
            <div className="lg:col-span-2">
              <Card>
                <CardHeader>
                  <CardTitle>Holdings</CardTitle>
                  <CardDescription>
                    Current portfolio positions and performance
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <HoldingsTable holdings={portfolioData.holdings} />
                </CardContent>
              </Card>
            </div>

            {/* Alerts and Actions */}
            <div className="space-y-6">
              <Card>
                <CardHeader>
                  <CardTitle>Performance Chart</CardTitle>
                </CardHeader>
                <CardContent>
                  <PerformanceChart />
                </CardContent>
              </Card>

              <Card>
                <CardHeader>
                  <CardTitle>Alerts</CardTitle>
                  <CardDescription>
                    Portfolio alerts and notifications
                  </CardDescription>
                </CardHeader>
                <CardContent>
                  <AlertsPanel holdings={portfolioData.holdings} />
                </CardContent>
              </Card>
            </div>
          </div>

          {/* Last Updated */}
          <div className="text-center text-sm text-gray-500">
            Last updated: {new Date(portfolioData.lastUpdated).toLocaleString()}
          </div>
        </>
      )}

      {!portfolioData && !isLoading && (
        <div className="text-center py-12">
          <p className="text-gray-500 mb-4">No portfolio data available</p>
          <button
            onClick={fetchPortfolioData}
            className="px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700"
          >
            Load Portfolio
          </button>
        </div>
      )}
    </div>
  );
} 