import { Entity, PrimaryGeneratedColumn, Column, OneToMany, Index } from 'typeorm';
import { Order } from './Order';

@Entity('users')
@Index(['email'])
export class User {
  @PrimaryGeneratedColumn()
  id!: number;

  @Column({ unique: true })
  email!: string;

  @Column({ default: true })
  @Index()
  active!: boolean;

  @OneToMany(() => Order, (o) => o.user, { eager: true })
  orders!: Order[];
}
