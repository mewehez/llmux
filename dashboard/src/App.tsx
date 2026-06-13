import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs"
import { WorkerStatus } from "@/components/WorkerStatus"
import { MessageSender } from "@/components/MessageSender"
import { LoadTester } from "@/components/LoadTester"

export default function App() {
  return (
    <div className="min-h-screen bg-background text-foreground p-6">
      <div className="max-w-5xl mx-auto space-y-6">
        <div>
          <h1 className="text-3xl font-bold">LLM Server Dashboard</h1>
          <p className="text-muted-foreground mt-1">
            Monitor workers · Send messages · Load test
          </p>
        </div>

        <Tabs defaultValue="status">
          <TabsList>
            <TabsTrigger value="status">Worker Status</TabsTrigger>
            <TabsTrigger value="chat">Message Sender</TabsTrigger>
            <TabsTrigger value="load">Load Tester</TabsTrigger>
          </TabsList>

          <TabsContent value="status">
            <WorkerStatus />
          </TabsContent>

          <TabsContent value="chat">
            <MessageSender />
          </TabsContent>

          <TabsContent value="load">
            <LoadTester />
          </TabsContent>
        </Tabs>
      </div>
    </div>
  )
}