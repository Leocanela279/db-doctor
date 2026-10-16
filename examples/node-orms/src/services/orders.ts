import { Like } from 'typeorm';
import { orderRepository, userRepository } from '../data-source';

export async function paidOrders() {
  const orders = await orderRepository.find({ where: { status: 'paid' }, order: { createdAt: 'DESC' } });
  const names: string[] = [];
  for (const order of orders) {
    const product = await order.product;
    names.push(product.name);
  }
  return names;
}

export async function searchUsers(q: string) {
  return userRepository.findBy({ email: Like(`%${q}%`) });
}
