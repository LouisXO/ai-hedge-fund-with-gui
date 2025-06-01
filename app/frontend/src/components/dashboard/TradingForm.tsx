import { useState } from 'react';
import { Button } from '../ui/button';
import { Input } from '../ui/input';
import { Label } from '../ui/label';
import { Textarea } from '../ui/textarea';
import { Checkbox } from '../ui/checkbox';

interface TradingFormProps {
  onSubmit: (formData: any) => void;
  isRunning: boolean;
}

const DEFAULT_ANALYSTS = [
  { id: 'financial_analyst', name: 'Financial Analyst', description: 'Fundamental analysis and financial metrics' },
  { id: 'technical_analyst', name: 'Technical Analyst', description: 'Chart patterns and technical indicators' },
  { id: 'sentiment_analyst', name: 'Sentiment Analyst', description: 'Market sentiment and news analysis' },
  { id: 'risk_analyst', name: 'Risk Analyst', description: 'Risk assessment and portfolio analysis' },
];

const DEFAULT_MODELS = [
  { id: 'gpt-4o', name: 'GPT-4o', provider: 'OpenAI' },
  { id: 'gpt-4o-mini', name: 'GPT-4o Mini', provider: 'OpenAI' },
  { id: 'claude-3-sonnet', name: 'Claude 3 Sonnet', provider: 'Anthropic' },
];

export function TradingForm({ onSubmit, isRunning }: TradingFormProps) {
  const [formData, setFormData] = useState({
    tickers: 'AAPL,MSFT,GOOGL',
    start_date: '',
    end_date: '',
    initial_cash: 100000,
    margin_requirement: 0,
    show_reasoning: true,
    selected_agents: ['financial_analyst', 'technical_analyst'],
    model_name: 'gpt-4o',
    model_provider: 'OpenAI',
  });

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    
    // Process form data
    const processedData = {
      ...formData,
      tickers: formData.tickers.split(',').map(t => t.trim().toUpperCase()),
      start_date: formData.start_date || undefined,
      end_date: formData.end_date || undefined,
    };
    
    onSubmit(processedData);
  };

  const handleAnalystChange = (analystId: string, checked: boolean) => {
    setFormData(prev => ({
      ...prev,
      selected_agents: checked 
        ? [...prev.selected_agents, analystId]
        : prev.selected_agents.filter(id => id !== analystId)
    }));
  };

  return (
    <form onSubmit={handleSubmit} className="space-y-6">
      {/* Stock Tickers */}
      <div className="space-y-2">
        <Label htmlFor="tickers">Stock Tickers</Label>
        <Input
          id="tickers"
          value={formData.tickers}
          onChange={(e) => setFormData(prev => ({ ...prev, tickers: e.target.value }))}
          placeholder="AAPL,MSFT,GOOGL"
          className="font-mono"
        />
        <p className="text-sm text-gray-500">Comma-separated ticker symbols</p>
      </div>

      {/* Date Range */}
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label htmlFor="start_date">Start Date</Label>
          <Input
            id="start_date"
            type="date"
            value={formData.start_date}
            onChange={(e) => setFormData(prev => ({ ...prev, start_date: e.target.value }))}
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="end_date">End Date</Label>
          <Input
            id="end_date"
            type="date"
            value={formData.end_date}
            onChange={(e) => setFormData(prev => ({ ...prev, end_date: e.target.value }))}
          />
        </div>
      </div>

      {/* Portfolio Settings */}
      <div className="grid grid-cols-2 gap-4">
        <div className="space-y-2">
          <Label htmlFor="initial_cash">Initial Cash ($)</Label>
          <Input
            id="initial_cash"
            type="number"
            value={formData.initial_cash}
            onChange={(e) => setFormData(prev => ({ ...prev, initial_cash: Number(e.target.value) }))}
            min="1000"
            step="1000"
          />
        </div>
        <div className="space-y-2">
          <Label htmlFor="margin_requirement">Margin Requirement (%)</Label>
          <Input
            id="margin_requirement"
            type="number"
            value={formData.margin_requirement}
            onChange={(e) => setFormData(prev => ({ ...prev, margin_requirement: Number(e.target.value) }))}
            min="0"
            max="100"
          />
        </div>
      </div>

      {/* AI Model Selection */}
      <div className="space-y-2">
        <Label>AI Model</Label>
        <select 
          value={formData.model_name}
          onChange={(e) => {
            const selectedModel = DEFAULT_MODELS.find(m => m.id === e.target.value);
            setFormData(prev => ({ 
              ...prev, 
              model_name: e.target.value,
              model_provider: selectedModel?.provider || 'OpenAI'
            }));
          }}
          className="w-full px-3 py-2 border border-gray-300 rounded-md focus:outline-none focus:ring-2 focus:ring-blue-500"
        >
          {DEFAULT_MODELS.map((model) => (
            <option key={model.id} value={model.id}>
              {model.name} ({model.provider})
            </option>
          ))}
        </select>
      </div>

      {/* Analyst Selection */}
      <div className="space-y-3">
        <Label>AI Analysts</Label>
        <div className="space-y-3">
          {DEFAULT_ANALYSTS.map((analyst) => (
            <div key={analyst.id} className="flex items-start space-x-3">
              <Checkbox
                id={analyst.id}
                checked={formData.selected_agents.includes(analyst.id)}
                onCheckedChange={(checked) => handleAnalystChange(analyst.id, checked as boolean)}
              />
              <div className="flex-1">
                <Label htmlFor={analyst.id} className="font-medium">{analyst.name}</Label>
                <p className="text-sm text-gray-500">{analyst.description}</p>
              </div>
            </div>
          ))}
        </div>
      </div>

      {/* Options */}
      <div className="flex items-center space-x-3">
        <Checkbox
          id="show_reasoning"
          checked={formData.show_reasoning}
          onCheckedChange={(checked) => setFormData(prev => ({ ...prev, show_reasoning: checked as boolean }))}
        />
        <Label htmlFor="show_reasoning">Show detailed reasoning</Label>
      </div>

      {/* Submit Button */}
      <Button 
        type="submit" 
        disabled={isRunning || formData.selected_agents.length === 0}
        className="w-full"
      >
        {isRunning ? 'Running Analysis...' : 'Run AI Analysis'}
      </Button>
    </form>
  );
} 