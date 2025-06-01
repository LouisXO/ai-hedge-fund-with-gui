import { useEffect, useRef } from 'react';

interface RealTimeUpdatesProps {
  updates: string[];
}

export function RealTimeUpdates({ updates }: RealTimeUpdatesProps) {
  const scrollRef = useRef<HTMLDivElement>(null);

  // Auto-scroll to bottom when new updates arrive
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [updates]);

  if (updates.length === 0) {
    return (
      <div className="text-center py-4 text-gray-500">
        Waiting for updates...
      </div>
    );
  }

  return (
    <div 
      ref={scrollRef}
      className="max-h-64 overflow-y-auto space-y-2 bg-gray-900 text-green-400 p-4 rounded-lg font-mono text-sm"
    >
      {updates.map((update, index) => (
        <div key={index} className="flex items-start gap-2">
          <span className="text-gray-500 text-xs mt-1">
            {new Date().toLocaleTimeString()}
          </span>
          <span className="flex-1">{update}</span>
        </div>
      ))}
    </div>
  );
} 