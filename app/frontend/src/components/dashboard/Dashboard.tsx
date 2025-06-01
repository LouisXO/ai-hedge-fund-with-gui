import { useState, useEffect } from 'react';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '../ui/card';
import { Button } from '../ui/button';
import { AgentDecisionsPanel } from './AgentDecisionsPanel';
import { TradingForm } from './TradingForm';
import { ResultsHistory } from './ResultsHistory';
import { RealTimeUpdates } from './RealTimeUpdates';

interface TradingDecision {
  agent: string;
  recommendation: 'BUY' | 'SELL' | 'HOLD';
  confidence: number;
  reasoning?: string;
  timestamp: string;
}

interface TradingResult {
  id: string;
  tickers: string[];
  decisions: TradingDecision[];
  portfolio: any;
  timestamp: string;
  performance?: {
    totalReturn: number;
    sharpeRatio: number;
    maxDrawdown: number;
  };
}

export function Dashboard() {
  const [isRunning, setIsRunning] = useState(false);
  const [currentResults, setCurrentResults] = useState<TradingResult | null>(null);
  const [resultsHistory, setResultsHistory] = useState<TradingResult[]>([]);
  const [streamingUpdates, setStreamingUpdates] = useState<string[]>([]);

  // Load results from localStorage on component mount
  useEffect(() => {
    const savedResults = localStorage.getItem('hedge-fund-results');
    if (savedResults) {
      try {
        setResultsHistory(JSON.parse(savedResults));
      } catch (error) {
        console.error('Failed to parse saved results:', error);
      }
    }
  }, []);

  // Save results to localStorage whenever resultsHistory changes
  useEffect(() => {
    localStorage.setItem('hedge-fund-results', JSON.stringify(resultsHistory));
  }, [resultsHistory]);

  const handleRunHedgeFund = async (formData: any) => {
    setIsRunning(true);
    setStreamingUpdates([]);
    
    try {
      const response = await fetch('http://localhost:8000/api/v1/hedge-fund/run', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(formData),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body reader available');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            try {
              const eventData = JSON.parse(line.slice(6));
              
              if (eventData.type === 'progress') {
                setStreamingUpdates(prev => [...prev, 
                  `${eventData.agent}: ${eventData.status} (${eventData.ticker || 'system'})`
                ]);
              } else if (eventData.type === 'complete') {
                const newResult: TradingResult = {
                  id: Date.now().toString(),
                  tickers: formData.tickers,
                  decisions: eventData.data.decisions || [],
                  portfolio: eventData.data.portfolio,
                  timestamp: new Date().toISOString(),
                };
                
                setCurrentResults(newResult);
                setResultsHistory(prev => [newResult, ...prev.slice(0, 9)]); // Keep last 10 results
              }
            } catch (parseError) {
              console.error('Failed to parse event data:', parseError);
            }
          }
        }
      }
    } catch (error) {
      console.error('Error running hedge fund:', error);
      setStreamingUpdates(prev => [...prev, `Error: ${error instanceof Error ? error.message : 'Unknown error'}`]);
    } finally {
      setIsRunning(false);
    }
  };

  return (
    <div className="p-6 space-y-6 max-w-7xl mx-auto">
      {/* Header */}
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-3xl font-bold tracking-tight">AI Hedge Fund Dashboard</h1>
          <p className="text-gray-600 dark:text-gray-400">
            Intelligent trading decisions powered by AI agents
          </p>
        </div>
        <div className="flex items-center gap-2">
          <div className={`w-3 h-3 rounded-full ${isRunning ? 'bg-green-500 animate-pulse' : 'bg-gray-400'}`} />
          <span className="text-sm text-gray-600">
            {isRunning ? 'Running' : 'Ready'}
          </span>
        </div>
      </div>

      {/* Main Content Grid */}
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column - Trading Form */}
        <div className="lg:col-span-1">
          <Card>
            <CardHeader>
              <CardTitle>Run Analysis</CardTitle>
              <CardDescription>
                Configure and execute AI trading analysis
              </CardDescription>
            </CardHeader>
            <CardContent>
              <TradingForm 
                onSubmit={handleRunHedgeFund}
                isRunning={isRunning}
              />
            </CardContent>
          </Card>

          {/* Real-time Updates */}
          {(isRunning || streamingUpdates.length > 0) && (
            <Card className="mt-6">
              <CardHeader>
                <CardTitle className="text-lg">Live Updates</CardTitle>
              </CardHeader>
              <CardContent>
                <RealTimeUpdates updates={streamingUpdates} />
              </CardContent>
            </Card>
          )}
        </div>

        {/* Right Columns - Results */}
        <div className="lg:col-span-2 space-y-6">
          {/* Current Results */}
          {currentResults && (
            <Card>
              <CardHeader>
                <CardTitle>Latest Analysis Results</CardTitle>
                <CardDescription>
                  {new Date(currentResults.timestamp).toLocaleString()}
                </CardDescription>
              </CardHeader>
              <CardContent>
                <AgentDecisionsPanel 
                  decisions={currentResults.decisions}
                  tickers={currentResults.tickers}
                />
              </CardContent>
            </Card>
          )}

          {/* Results History */}
          <Card>
            <CardHeader>
              <CardTitle>Analysis History</CardTitle>
              <CardDescription>
                Previous trading analysis results
              </CardDescription>
            </CardHeader>
            <CardContent>
              <ResultsHistory 
                results={resultsHistory}
                onSelectResult={setCurrentResults}
              />
            </CardContent>
          </Card>
        </div>
      </div>
    </div>
  );
} 